"""HTTP routes. Error bodies are short fixed strings; upstream details stay in the logs."""

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse

from app.analytics.metrics import contributor_metrics
from app.analytics.periods import DAY, Period, windows
from app.api.ratelimit import enforce_rate_limit
from app.github.client import GitHubError, NotFound, RateLimited
from app.github.validation import LOGIN_PATTERN
from app.models import ContributorResponse, Meta, WindowRange
from app.services import Services
from app.store.records import SyncStatus
from app.sync.contributor_sync import NewContributorCapReached

logger = logging.getLogger(__name__)

RETRY_AFTER_SECONDS = 10

router = APIRouter(prefix="/api", dependencies=[Depends(enforce_rate_limit)])

Login = Annotated[str, Path(pattern=LOGIN_PATTERN.pattern, description="GitHub username")]


@router.get(
    "/contributors/{login}",
    response_model=ContributorResponse,
    responses={202: {"description": "History is still being fetched"},
               404: {"description": "Unknown user, or no activity in the repo"},
               429: {"description": "Rate limit or new-contributor cap reached"},
               503: {"description": "GitHub unavailable and nothing cached"}},
)
async def get_contributor(request: Request, login: Login,
                          period: Annotated[Period, Query()] = Period.MONTH):
    services: Services = request.app.state.services
    settings = request.app.state.settings
    if services.sync is None:
        raise HTTPException(503, "Data source unavailable")

    try:
        result = await services.sync.ensure_fresh(login)
    except NotFound:
        raise HTTPException(404, "GitHub user not found") from None
    except NewContributorCapReached:
        raise HTTPException(429, "Too many new contributors this hour; try again later",
                            headers={"Retry-After": "3600"}) from None
    except RateLimited:
        raise HTTPException(503, "GitHub rate limit reached; try again later") from None
    except GitHubError as exc:
        logger.warning("GitHub error for %s: %s", login, exc)
        raise HTTPException(503, "GitHub unavailable") from None

    state = result.state
    if state.status is SyncStatus.FETCHING:
        return JSONResponse(
            status_code=202,
            content={"status": "fetching", "progress": state.progress},
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )
    if state.status is not SyncStatus.READY:
        raise HTTPException(503, "Fetching contributor data failed; try again later")

    now = int(services.clock())
    current, previous = windows(period, now)
    history_start = now - settings.history_days * DAY
    prs = await services.repo.get_merged_prs(state.login, history_start, now)
    reviews = await services.repo.get_reviews_given(state.login, history_start, now)
    if not prs and not reviews:
        raise HTTPException(404, f"No activity in {settings.target_repo}")

    def within(window, items, ts):
        return [item for item in items if window.contains(ts(item))]

    metrics = contributor_metrics(
        period, current, previous,
        within(current, prs, lambda p: p.merged_at), within(current, reviews, lambda r: r.submitted_at),
        within(previous, prs, lambda p: p.merged_at), within(previous, reviews, lambda r: r.submitted_at),
    )
    return ContributorResponse(
        repo=settings.target_repo,
        login=state.login,
        period=period,
        window=WindowRange(from_=_date(current.start), to=_date(current.end)),
        metrics=metrics,
        meta=Meta(
            last_synced_at=datetime.fromtimestamp(state.last_sync_at, UTC) if state.last_sync_at else None,
            stale=result.stale,
        ),
    )


def _date(epoch: int):
    return datetime.fromtimestamp(epoch, UTC).date()
