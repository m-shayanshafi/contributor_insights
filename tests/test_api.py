from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from app.analytics.periods import DAY
from app.github.client import NotFound, RateLimited, Upstream
from app.llm.base import LLMError
from app.main import create_app
from app.models import NarrativeLLMOutput
from app.narrative.service import NarrativeService
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


class StubLLM:
    model = "stub-model"

    def __init__(self):
        self.calls = 0
        self.result: NarrativeLLMOutput | Exception = NarrativeLLMOutput(
            narrative="Output doubled.", self_comparison="Faster",
            root_cause_hypothesis="More small fixes", confidence=0.8,
            evidence_chain=[
                {"claim": "PRs doubled", "ref": "metrics.prs_merged.pct_change", "value": "100.0"},
                {"claim": "made up", "ref": "metrics.prs_merged.value", "value": "42"},
            ],
        )

    async def generate(self, system, user, key):
        self.calls += 1
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
    llm = StubLLM()

    @asynccontextmanager
    async def services_factory(settings):
        conn = await connect(tmp_path / "api.db")
        repo = Repository(conn)
        await seed(repo)
        yield Services(repo=repo, sync=sync, clock=lambda: NOW,
                       narratives=NarrativeService(repo, llm, settings.target_repo))
        await conn.close()

    def make(**settings):
        app = create_app(make_settings(**settings), services_factory=services_factory)
        return TestClient(app)

    make.llm = llm
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


# Narrative endpoint

def test_narrative_response_is_grounded_and_capped(api):
    make, _ = api
    with make() as client:
        response = client.get("/api/contributors/alice/narrative?period=1m")
    assert response.status_code == 200
    body = response.json()
    assert body["narrative"] == "Output doubled."
    assert [e["ref"] for e in body["evidence_chain"]] == ["metrics.prs_merged.pct_change"]
    assert body["ungrounded_evidence_dropped"] == 1
    # 2 + 1 PRs and 1 review across both windows: sufficiency 4/20 caps the 0.8 confidence.
    assert (body["confidence"], body["llm_confidence"], body["data_sufficiency"]) == (0.2, 0.8, 0.2)
    assert body["metrics"]["prs_merged"]["value"] == 2
    assert body["meta"]["cached"] is False and body["meta"]["model"] == "stub-model"


def test_narrative_cached_for_unchanged_input(api):
    make, _ = api
    with make() as client:
        first = client.get("/api/contributors/alice/narrative").json()
        second = client.get("/api/contributors/alice/narrative").json()
        other_period = client.get("/api/contributors/alice/narrative?period=1w").json()
    assert make.llm.calls == 2  # 1m once, then 1w
    assert second["meta"]["cached"] is True and other_period["meta"]["cached"] is False
    assert {k: v for k, v in second.items() if k != "meta"} == \
           {k: v for k, v in first.items() if k != "meta"}


def test_llm_failure_returns_502_with_metrics(api):
    make, _ = api
    make.llm.result = LLMError("Gemini API error 429")
    with make() as client:
        response = client.get("/api/contributors/alice/narrative")
    assert response.status_code == 502
    body = response.json()
    assert body["detail"] == "Narrative generation failed; metrics are included"
    assert body["metrics"]["prs_merged"]["value"] == 2
    assert "429" not in response.text


def test_narrative_shares_sync_error_mapping(api):
    make, sync = api
    sync.result = NotFound("user not found")
    with make() as client:
        assert client.get("/api/contributors/alice/narrative").status_code == 404
    assert make.llm.calls == 0
