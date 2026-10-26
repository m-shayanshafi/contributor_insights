from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from app.analytics.periods import DAY
from app.github.client import NotFound, RateLimited, Upstream
from app.main import create_app
from app.services import Services
from app.store.db import connect
from app.store.records import ContributorState, PullRequest, Review, SyncStatus
from app.store.repository import Repository
from app.sync.contributor_sync import NewContributorCapReached, SyncResult

NOW = 1_800_000_000  # 2027-01-15T08:00:00Z


def ready(login="Alice", stale=False):
    return SyncResult(ContributorState(login=login, status=SyncStatus.READY, progress=1.0,
                                       created_at=NOW - DAY, last_sync_at=NOW - 60), stale=stale)


class FakeSync:
    def __init__(self):
        self.result: SyncResult | Exception = ready()
        self.logins: list[str] = []

    async def ensure_fresh(self, login):
        self.logins.append(login)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def seed(repo: Repository):
    await repo.upsert_pull_requests([
        PullRequest(number=n, author_login="Alice", title="t", created_at=merged - 3600,
                    updated_at=merged, state="MERGED", additions=100, deletions=20, merged_at=merged)
        for n, merged in [(1, NOW - 2 * DAY), (2, NOW - 3 * DAY), (3, NOW - 40 * DAY)]
    ])
    await repo.replace_reviews(9, "Alice", [
        Review(review_id="R1", pr_number=9, pr_author_login="bob", reviewer_login="Alice",
               state="APPROVED", submitted_at=NOW - DAY),
    ])


@pytest.fixture
def api(make_settings, tmp_path):
    sync = FakeSync()

    @asynccontextmanager
    async def services_factory(settings):
        conn = await connect(tmp_path / "api.db")
        repo = Repository(conn)
        await seed(repo)
        yield Services(repo=repo, sync=sync, clock=lambda: NOW)
        await conn.close()

    def make(**settings):
        app = create_app(make_settings(**settings), services_factory=services_factory)
        return TestClient(app)

    return make, sync


def test_contributor_metrics_response(api):
    make, sync = api
    with make() as client:
        response = client.get("/api/contributors/alice?period=1m")
    assert response.status_code == 200
    body = response.json()
    assert body["repo"] == "home-assistant/core"
    assert body["login"] == "Alice"
    assert body["period"] == "1m"
    assert body["window"] == {"from": "2026-12-16", "to": "2027-01-15"}
    metrics = body["metrics"]
    assert metrics["prs_merged"] == {"value": 2, "previous_period": 1, "pct_change": 100.0}
    assert metrics["lines_changed"]["value"] == 240
    assert metrics["reviews_given"]["value"] == 1
    assert len(metrics["series"]) == 5  # 30 days in weekly buckets
    assert body["meta"] == {"last_synced_at": "2027-01-15T07:59:00Z", "stale": False}
    assert sync.logins == ["alice"]


def test_default_period_is_one_month(api):
    make, _ = api
    with make() as client:
        assert client.get("/api/contributors/alice").json()["period"] == "1m"


def test_stale_flag_passed_through(api):
    make, sync = api
    sync.result = ready(stale=True)
    with make() as client:
        assert client.get("/api/contributors/alice").json()["meta"]["stale"] is True


def test_fetching_returns_202_with_progress(api):
    make, sync = api
    sync.result = SyncResult(ContributorState(login="Alice", status=SyncStatus.FETCHING,
                                              progress=0.42, created_at=NOW))
    with make() as client:
        response = client.get("/api/contributors/alice")
    assert response.status_code == 202
    assert response.json() == {"status": "fetching", "progress": 0.42}
    assert response.headers["Retry-After"] == "10"


@pytest.mark.parametrize("path", [
    "/api/contributors/x%20repo:evil",
    "/api/contributors/dependabot[bot]",
    "/api/contributors/" + "a" * 40,
    "/api/contributors/alice?period=6m",
])
def test_invalid_input_is_422_and_never_synced(api, path):
    make, sync = api
    with make() as client:
        assert client.get(path).status_code == 422
    assert sync.logins == []


@pytest.mark.parametrize("error, status, detail", [
    (NotFound("user not found"), 404, "GitHub user not found"),
    (NewContributorCapReached(), 429, "Too many new contributors this hour; try again later"),
    (RateLimited("limit"), 503, "GitHub rate limit reached; try again later"),
    (Upstream("GitHub returned 502: secret body"), 503, "GitHub unavailable"),
])
def test_errors_map_to_status_codes(api, error, status, detail):
    make, sync = api
    sync.result = error
    with make() as client:
        response = client.get("/api/contributors/alice")
    assert response.status_code == status
    assert response.json() == {"detail": detail}


def test_no_activity_is_404(api):
    make, sync = api
    sync.result = ready(login="Carol")
    with make() as client:
        response = client.get("/api/contributors/carol")
    assert response.status_code == 404
    assert response.json() == {"detail": "No activity in home-assistant/core"}


def test_rate_limit_per_client(api):
    make, _ = api
    with make(rate_limit="2/minute") as client:
        assert client.get("/api/contributors/alice").status_code == 200
        assert client.get("/api/contributors/alice").status_code == 200
        response = client.get("/api/contributors/alice")
        assert response.status_code == 429
        assert response.json() == {"detail": "Rate limit exceeded"}
        assert int(response.headers["Retry-After"]) > 0
        assert client.get("/api/contributors/bob").status_code == 429  # per client, not per path
        assert client.get("/healthz").status_code == 200  # exempt
