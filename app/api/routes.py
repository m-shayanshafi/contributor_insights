"""HTTP routes. Error bodies are short fixed strings; upstream details stay in the logs."""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse

from app.analytics.metrics import contributor_metrics
from app.analytics.periods import DAY, Period, windows
from app.analytics.window_records import WindowRecords, load_window_records
from app.api.ratelimit import enforce_rate_limit
from app.github.client import GitHubError, NotFound, RateLimited
from app.github.validation import LOGIN_PATTERN
from app.llm.base import LLMError
from app.models import (
    ContributorMetrics,
    ContributorResponse,
    Meta,
    NarrativeMeta,
    NarrativeResponse,
    WindowRange,
)
from app.services import Services
from app.store.records import ContributorState, SyncStatus
from app.sync.contributor_sync import NewContributorCapReached

logger = logging.getLogger(__name__)

RETRY_AFTER_SECONDS = 10

router = APIRouter(prefix="/api", dependencies=[Depends(enforce_rate_limit)])

Login = Annotated[str, Path(pattern=LOGIN_PATTERN.pattern, description="GitHub username")]
PeriodQuery = Annotated[Period, Query()]

COMMON_RESPONSES = {
    202: {"description": "History is still being fetched"},
    404: {"description": "Unknown user, or no activity in the repo"},
    429: {"description": "Rate limit or new-contributor cap reached"},
    503: {"description": "GitHub unavailable and nothing cached"},
}


@dataclass(frozen=True)
class ContributorData:
    state: ContributorState
    stale: bool
    now: int
    window: WindowRange
    records: WindowRecords
    metrics: ContributorMetrics

    def meta(self) -> dict:
        synced = self.state.last_sync_at
        return {"last_synced_at": datetime.fromtimestamp(synced, UTC) if synced else None,
                "stale": self.stale}


@router.get("/contributors/{login}", response_model=ContributorResponse,
            responses=COMMON_RESPONSES)
async def get_contributor(request: Request, login: Login, period: PeriodQuery = Period.MONTH):
    data = await _contributor_data(request, login, period)
    if isinstance(data, JSONResponse):
        return data
    return ContributorResponse(
        repo=request.app.state.settings.target_repo, login=data.state.login, period=period,
        window=data.window, metrics=data.metrics, meta=Meta(**data.meta()),
    )


@router.get("/contributors/{login}/narrative", response_model=NarrativeResponse,
            responses={**COMMON_RESPONSES,
                       502: {"description": "The LLM failed; the body still has the metrics"}})
async def get_narrative(request: Request, login: Login, period: PeriodQuery = Period.MONTH):
    data = await _contributor_data(request, login, period)
    if isinstance(data, JSONResponse):
        return data
    services: Services = request.app.state.services
    try:
        narrative = await services.narratives.narrate(
            data.state.login, period, data.window.from_, data.window.to,
            data.metrics, data.records, data.now)
    except LLMError as exc:
        logger.warning("Narrative failed for %s: %s", data.state.login, exc)
        return JSONResponse(status_code=502, content={
            "detail": "Narrative generation failed; metrics are included",
            "metrics": data.metrics.model_dump(mode="json"),
        })
    fields = dict(narrative.__dict__)
    meta = NarrativeMeta(**data.meta(), cached=fields.pop("cached"), model=fields.pop("model"))
    return NarrativeResponse(
        repo=request.app.state.settings.target_repo, login=data.state.login, period=period,
        window=data.window, metrics=data.metrics, meta=meta, **fields,
    )


async def _contributor_data(request: Request, login: str,
                            period: Period) -> ContributorData | JSONResponse:
    """Sync the contributor and compute their metrics, mapping failures to HTTP errors."""
    services: Services = request.app.state.services
    settings = request.app.state.settings
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
    history_start = now - settings.history_days * DAY
    if not (await services.repo.get_merged_prs(state.login, history_start, now)
            or await services.repo.get_reviews_given(state.login, history_start, now)):
        raise HTTPException(404, f"No activity in {settings.target_repo}")

    current, previous = windows(period, now)
    records = await load_window_records(services.repo, state.login, current, previous)
    metrics = contributor_metrics(period, current, previous, records.current_prs,
                                  records.current_reviews, records.previous_prs,
                                  records.previous_reviews)
    return ContributorData(
        state=state, stale=result.stale, now=now,
        window=WindowRange(from_=_date(current.start), to=_date(current.end)),
        records=records, metrics=metrics,
    )


def _date(epoch: int) -> date:
    return datetime.fromtimestamp(epoch, UTC).date()
