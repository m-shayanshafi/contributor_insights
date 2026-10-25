import pytest

from app.store.db import connect
from app.store.records import PullRequest, Review, SyncStatus
from app.store.repository import Repository

DAY = 86_400


@pytest.fixture
async def repo(tmp_path):
    conn = await connect(tmp_path / "test.db")
    yield Repository(conn)
    await conn.close()


def make_pr(number, author="alice", merged_at=None, updated_at=None, additions=10, title="Fix"):
    return PullRequest(
        number=number, author_login=author, title=title, created_at=0,
        updated_at=updated_at if updated_at is not None else (merged_at or 0),
        state="MERGED" if merged_at is not None else "OPEN",
        additions=additions, deletions=5, merged_at=merged_at,
    )


def make_review(review_id, pr_number=1, reviewer="alice", pr_author="bob", submitted_at=100):
    return Review(
        review_id=review_id, pr_number=pr_number, pr_author_login=pr_author,
        reviewer_login=reviewer, state="APPROVED", submitted_at=submitted_at,
    )


# Contributors

async def test_contributor_lifecycle(repo):
    assert await repo.get_contributor("alice") is None
    await repo.create_contributor("alice", now=1000)
    state = await repo.get_contributor("alice")
    assert state.status is SyncStatus.FETCHING and state.progress == 0

    await repo.update_contributor("alice", status=SyncStatus.READY, progress=1.0, last_sync_at=2000)
    state = await repo.get_contributor("alice")
    assert (state.status, state.progress, state.last_sync_at) == (SyncStatus.READY, 1.0, 2000)


async def test_login_lookup_is_case_insensitive(repo):
    await repo.create_contributor("Frenck", now=0)
    assert (await repo.get_contributor("frenck")).login == "Frenck"


async def test_update_rejects_unknown_columns(repo):
    await repo.create_contributor("alice", now=0)
    with pytest.raises(ValueError, match="Cannot update"):
        await repo.update_contributor("alice", login="mallory")
    with pytest.raises(ValueError, match="Cannot update"):
        await repo.update_contributor("alice", **{"status = 'ready'; --": 1})


async def test_count_contributors_created_since(repo):
    await repo.create_contributor("old", now=100)
    await repo.create_contributor("new1", now=5000)
    await repo.create_contributor("new2", now=6000)
    assert await repo.count_contributors_created_since(5000) == 2


# Pull requests

async def test_upsert_is_idempotent_and_updates(repo):
    await repo.upsert_pull_requests([make_pr(1, merged_at=None, updated_at=10)])
    await repo.upsert_pull_requests([make_pr(1, merged_at=50, additions=99)])
    await repo.upsert_pull_requests([make_pr(1, merged_at=50, additions=99)])
    prs = await repo.get_merged_prs("alice", 0, 100)
    assert len(prs) == 1
    assert prs[0].merged_at == 50 and prs[0].additions == 99


async def test_merged_prs_window_is_half_open(repo):
    await repo.upsert_pull_requests([
        make_pr(1, merged_at=99), make_pr(2, merged_at=100),
        make_pr(3, merged_at=199), make_pr(4, merged_at=200),
        make_pr(5, merged_at=None, updated_at=150), make_pr(6, author="bob", merged_at=150),
    ])
    prs = await repo.get_merged_prs("alice", 100, 200)
    assert [pr.number for pr in prs] == [2, 3]


async def test_prs_with_titles_containing_sql_are_stored_verbatim(repo):
    title = "Robert'); DROP TABLE pull_requests;--"
    await repo.upsert_pull_requests([make_pr(1, merged_at=10, title=title)])
    assert (await repo.get_merged_prs("alice", 0, 100))[0].title == title


# Reviews

async def test_reviews_exclude_self_reviews_and_respect_window(repo):
    await repo.replace_reviews(1, "alice", [make_review("r1", submitted_at=100)])
    await repo.replace_reviews(2, "alice", [make_review("r2", pr_number=2, pr_author="alice")])
    await repo.replace_reviews(3, "alice", [make_review("r3", pr_number=3, submitted_at=500)])
    reviews = await repo.get_reviews_given("alice", 0, 200)
    assert [r.review_id for r in reviews] == ["r1"]


async def test_replace_reviews_only_touches_one_reviewer(repo):
    await repo.replace_reviews(1, "alice", [make_review("a1"), make_review("a2")])
    await repo.replace_reviews(1, "carol", [make_review("c1", reviewer="carol")])
    await repo.replace_reviews(1, "alice", [make_review("a3")])
    assert [r.review_id for r in await repo.get_reviews_given("alice", 0, 1000)] == ["a3"]
    assert [r.review_id for r in await repo.get_reviews_given("carol", 0, 1000)] == ["c1"]


# Retention

async def test_retention_keeps_prs_merged_after_cutoff(repo):
    cutoff = 100 * DAY
    await repo.upsert_pull_requests([
        make_pr(1, merged_at=cutoff - 1),               # merged before cutoff: dropped
        make_pr(2, merged_at=cutoff + DAY),             # merged after cutoff: kept
        make_pr(3, merged_at=None, updated_at=cutoff - 1),  # stale open PR: dropped
        make_pr(4, merged_at=None, updated_at=cutoff + 1),  # active open PR: kept
    ])
    await repo.replace_reviews(1, "alice", [make_review("old", submitted_at=cutoff - 1)])
    await repo.replace_reviews(2, "alice", [make_review("new", pr_number=2, submitted_at=cutoff)])

    await repo.delete_older_than(cutoff)

    assert [pr.number for pr in await repo.get_merged_prs("alice", 0, 10**10)] == [2]
    assert [r.review_id for r in await repo.get_reviews_given("alice", 0, 10**10)] == ["new"]


# Narrative cache

async def test_narrative_cache_keyed_by_hash(repo):
    assert await repo.get_narrative("alice", "1m", "h1") is None
    await repo.put_narrative("alice", "1m", "h1", '{"narrative": "a"}', now=0)
    await repo.put_narrative("alice", "1m", "h2", '{"narrative": "b"}', now=0)
    assert await repo.get_narrative("ALICE", "1m", "h1") == '{"narrative": "a"}'
    assert await repo.get_narrative("alice", "3m", "h1") is None
