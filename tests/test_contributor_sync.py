import asyncio

import pytest

from app.analytics.periods import DAY
from app.github.client import NotFound, Upstream
from app.github.queries import ReviewedPR
from app.github.validation import InvalidLogin
from app.store.db import connect
from app.store.records import PullRequest, Review, SyncStatus
from app.store.repository import Repository
from app.sync.contributor_sync import ContributorSync, NewContributorCapReached

NOW = 1_800_000_000


def make_pr(number, updated, merged=True, author="alice"):
    return PullRequest(number=number, author_login=author, title=f"PR {number}",
                       created_at=updated - DAY, updated_at=updated,
                       state="MERGED" if merged else "OPEN", additions=10, deletions=5,
                       merged_at=updated if merged else None)


def make_review(review_id, pr_number, submitted):
    return Review(review_id=review_id, pr_number=pr_number, pr_author_login="bob",
                  reviewer_login="alice", state="APPROVED", submitted_at=submitted)


class FakeQueries:
    """Serves PRs and reviews by updated time, like the real searches."""

    def __init__(self):
        self.prs: list[PullRequest] = []
        self.reviews: list[Review] = []
        self.calls: list[tuple[str, int, int]] = []
        self.fail: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def authored_prs(self, login, start, end):
        self.calls.append(("authored", start, end))
        if self.gate:
            await self.gate.wait()
        if self.fail:
            raise self.fail
        return [pr for pr in self.prs if start <= pr.updated_at < end]

    async def reviewed_prs(self, login, start, end):
        self.calls.append(("reviewed", start, end))
        if self.fail:
            raise self.fail
        by_pr: dict[int, list[Review]] = {}
        for review in self.reviews:
            if start <= review.submitted_at < end:
                by_pr.setdefault(review.pr_number, []).append(review)
        return [ReviewedPR(number, reviews) for number, reviews in by_pr.items()]


class FakeUsers:
    def __init__(self):
        self.calls = 0

    async def get_user(self, login):
        self.calls += 1
        if login == "ghost-user":
            raise NotFound("user not found")
        return login.capitalize()


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
async def env(tmp_path, make_settings):
    conn = await connect(tmp_path / "sync.db")
    repo = Repository(conn)
    queries, users, clock = FakeQueries(), FakeUsers(), Clock()
    sync = ContributorSync(repo, queries, users,
                           make_settings(max_new_contributors_per_hour=3), clock=clock)
    yield sync, repo, queries, users, clock
    await sync.aclose()
    await conn.close()


async def test_new_login_backfills_then_ready(env):
    sync, repo, queries, users, _ = env
    queries.prs = [make_pr(1, NOW - 10 * DAY), make_pr(2, NOW - 400 * DAY)]
    queries.reviews = [make_review("R1", 50, NOW - 5 * DAY)]

    first = await sync.ensure_fresh("alice")
    assert first.state.status is SyncStatus.FETCHING
    assert first.state.login == "Alice"

    await sync.wait_for_backfills()
    state = (await sync.ensure_fresh("alice")).state
    assert state.status is SyncStatus.READY
    assert state.progress == 1.0
    assert state.covered_since == NOW - 180 * DAY
    assert [p.number for p in await repo.get_merged_prs("alice", 0, NOW)] == [1]
    assert [r.review_id for r in await repo.get_reviews_given("alice", 0, NOW)] == ["R1"]
    # 180 days = six 30-day slices, searched once for PRs and once for reviews.
    assert len(queries.calls) == 12


async def test_no_github_calls_within_ttl(env):
    sync, _, queries, _, clock = env
    await sync.ensure_fresh("alice")
    await sync.wait_for_backfills()
    calls = len(queries.calls)
    clock.now += 3599
    result = await sync.ensure_fresh("alice")
    assert result.state.status is SyncStatus.READY and not result.stale
    assert len(queries.calls) == calls


async def test_incremental_picks_up_late_merge(env):
    sync, repo, queries, _, clock = env
    queries.prs = [make_pr(7, NOW - DAY, merged=False)]
    await sync.ensure_fresh("alice")
    await sync.wait_for_backfills()
    assert await repo.get_merged_prs("alice", 0, NOW + DAY) == []

    clock.now += 2 * 3600
    queries.prs = [make_pr(7, clock.now - 60)]  # merged after the backfill
    queries.calls.clear()
    state = (await sync.ensure_fresh("alice")).state

    assert queries.calls == [("authored", NOW, clock.now), ("reviewed", NOW, clock.now)]
    assert [p.number for p in await repo.get_merged_prs("alice", 0, clock.now)] == [7]
    assert state.last_sync_at == clock.now and state.prs_watermark == clock.now


async def test_github_failure_serves_stale_cache(env):
    sync, _, queries, _, clock = env
    await sync.ensure_fresh("alice")
    await sync.wait_for_backfills()
    clock.now += 2 * 3600
    queries.fail = Upstream("GitHub returned 503")
    result = await sync.ensure_fresh("alice")
    assert result.stale
    assert result.state.last_sync_at == NOW


async def test_failed_backfill_records_error_then_retries(env):
    sync, repo, queries, _, _ = env
    queries.fail = Upstream("GitHub returned 503")
    await sync.ensure_fresh("alice")
    await sync.wait_for_backfills()
    state = await repo.get_contributor("alice")
    assert state.status is SyncStatus.ERROR and state.error == "GitHub returned 503"

    queries.fail = None
    retry = await sync.ensure_fresh("alice")
    assert retry.state.status is SyncStatus.FETCHING
    await sync.wait_for_backfills()
    assert (await sync.ensure_fresh("alice")).state.status is SyncStatus.READY


async def test_concurrent_requests_start_one_backfill(env):
    sync, _, queries, users, _ = env
    queries.gate = asyncio.Event()
    results = await asyncio.gather(*(sync.ensure_fresh("alice") for _ in range(5)))
    assert all(r.state.status is SyncStatus.FETCHING for r in results)
    assert users.calls == 1
    queries.gate.set()
    await sync.wait_for_backfills()
    assert len(queries.calls) == 12


async def test_new_contributor_cap(env):
    sync, _, _, _, _ = env
    for login in ("a1", "a2", "a3"):
        await sync.ensure_fresh(login)
    with pytest.raises(NewContributorCapReached):
        await sync.ensure_fresh("a4")
    await sync.ensure_fresh("a1")  # existing logins are unaffected


async def test_unknown_user_is_not_stored(env):
    sync, repo, _, _, _ = env
    with pytest.raises(NotFound):
        await sync.ensure_fresh("ghost-user")
    assert await repo.get_contributor("ghost-user") is None


async def test_backfills_respect_concurrency_limit(env):
    sync, _, queries, _, _ = env  # make_settings default: 2 concurrent backfills
    queries.gate = asyncio.Event()
    for login in ("a1", "a2", "a3"):
        await sync.ensure_fresh(login)
    await asyncio.sleep(0)
    assert len(queries.calls) == 2
    queries.gate.set()
    await sync.wait_for_backfills()


async def test_invalid_login_rejected(env):
    sync, *_ = env
    with pytest.raises(InvalidLogin):
        await sync.ensure_fresh("x repo:evil/repo")
