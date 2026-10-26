from app.analytics.periods import DAY, Period, windows
from app.analytics.window_records import load_window_records
from app.store.db import connect
from app.store.records import PullRequest, Review
from app.store.repository import Repository

NOW = 1_800_000_000


async def test_records_split_by_window(tmp_path):
    conn = await connect(tmp_path / "w.db")
    repo = Repository(conn)
    current, previous = windows(Period.WEEK, NOW)
    await repo.upsert_pull_requests([
        PullRequest(number=n, author_login="alice", title="t", created_at=t - 60, updated_at=t,
                    state="MERGED", additions=1, deletions=0, merged_at=t)
        for n, t in [(1, NOW - DAY), (2, NOW - 8 * DAY), (3, NOW - 20 * DAY)]
    ])
    await repo.replace_reviews(9, "alice", [Review(review_id="R", pr_number=9, pr_author_login="bob",
                                                   reviewer_login="alice", state="APPROVED",
                                                   submitted_at=NOW - 2 * DAY)])
    records = await load_window_records(repo, "alice", current, previous)
    assert [p.number for p in records.current_prs] == [1]
    assert [p.number for p in records.previous_prs] == [2]  # PR 3 is outside both windows
    assert [r.review_id for r in records.current_reviews] == ["R"]
    assert records.previous_reviews == [] and records.activity == 3
    await conn.close()
