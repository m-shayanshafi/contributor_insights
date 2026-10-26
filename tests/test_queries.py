import re
from datetime import UTC, datetime

import pytest

from app.analytics.periods import DAY
from app.github.queries import (
    AUTHORED_QUERY,
    SLICE_SECONDS,
    ContributorQueries,
    monthly_slices,
    search_string,
)
from app.github.validation import InvalidLogin

REPO = "home-assistant/core"
START = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def pr_node(number, updated, author="alice", merged=True, reviews=()):
    return {
        "number": number, "title": f"PR {number}", "state": "MERGED" if merged else "OPEN",
        "additions": 10, "deletions": 5,
        "createdAt": iso(updated - DAY), "updatedAt": iso(updated),
        "mergedAt": iso(updated) if merged else None, "closedAt": iso(updated) if merged else None,
        "author": {"login": author} if author else None,
        "reviews": {"nodes": list(reviews)},
    }


class FakeGitHub:
    """Answers searches from an in-memory list, honouring the updated: range and cursors."""

    def __init__(self, nodes):
        self.nodes = nodes
        self.calls = []

    async def graphql(self, query, variables):
        self.calls.append((query, dict(variables)))
        low, high = re.search(r"updated:(\S+)\.\.(\S+)", variables["q"]).groups()
        low, high = (int(datetime.fromisoformat(v).timestamp()) for v in (low, high))
        matches = [n for n in self.nodes
                   if low <= int(datetime.fromisoformat(n["updatedAt"]).timestamp()) <= high]
        offset = int(variables["cursor"] or 0)
        page = matches[offset:offset + variables["first"]]
        next_offset = offset + len(page)
        return {"search": {
            "issueCount": len(matches),
            "pageInfo": {"hasNextPage": next_offset < len(matches), "endCursor": str(next_offset)},
            "nodes": page,
        }}


def test_monthly_slices_cover_range_without_gaps():
    end = START + 75 * DAY
    slices = list(monthly_slices(START, end))
    assert slices == [(START, START + SLICE_SECONDS), (START + SLICE_SECONDS, START + 2 * SLICE_SECONDS),
                      (START + 2 * SLICE_SECONDS, end)]


def test_search_string_shape():
    q = search_string(REPO, "reviewed", "alice", START, START + DAY)
    assert q == ("repo:home-assistant/core is:pr reviewed-by:alice "
                 "updated:2026-01-01T00:00:00Z..2026-01-01T23:59:59Z sort:updated-asc")


async def test_authored_prs_parsed():
    fake = FakeGitHub([pr_node(1, START + DAY), pr_node(2, START + 2 * DAY, merged=False)])
    prs = await ContributorQueries(fake, REPO).authored_prs("alice", START, START + 10 * DAY)
    assert [p.number for p in prs] == [1, 2]
    first = prs[0]
    assert first.author_login == "alice"
    assert first.merged_at == START + DAY
    assert first.created_at == START
    assert first.lines_changed == 15
    assert prs[1].merged_at is None and prs[1].closed_at is None


async def test_paginates_through_all_pages():
    nodes = [pr_node(n, START + n * 60) for n in range(1, 251)]
    fake = FakeGitHub(nodes)
    prs = await ContributorQueries(fake, REPO).authored_prs("alice", START, START + DAY)
    assert len(prs) == 250
    assert [v["cursor"] for _, v in fake.calls] == [None, "100", "200"]


async def test_slice_over_cap_is_split():
    # 12 PRs in one month with a cap of 5 forces recursive halving.
    nodes = [pr_node(n, START + n * 2 * DAY) for n in range(1, 13)]
    fake = FakeGitHub(nodes)
    queries = ContributorQueries(fake, REPO, page_size=100, result_cap=5)
    prs = await queries.authored_prs("alice", START, START + SLICE_SECONDS)
    assert [p.number for p in prs] == list(range(1, 13))
    assert len(fake.calls) > 1


async def test_multiple_months_are_searched_separately():
    nodes = [pr_node(1, START + DAY), pr_node(2, START + 40 * DAY), pr_node(3, START + 80 * DAY)]
    fake = FakeGitHub(nodes)
    prs = await ContributorQueries(fake, REPO).authored_prs("alice", START, START + 90 * DAY)
    assert [p.number for p in prs] == [1, 2, 3]
    assert len(fake.calls) == 3


async def test_reviews_parsed_and_self_reviews_and_pending_skipped():
    review = {"id": "R1", "state": "APPROVED", "submittedAt": iso(START + DAY)}
    pending = {"id": "R2", "state": "PENDING", "submittedAt": None}
    nodes = [
        pr_node(10, START + DAY, author="bob", reviews=[review, pending]),
        pr_node(11, START + DAY, author="Alice", reviews=[{**review, "id": "R3"}]),
        pr_node(12, START + DAY, author=None, reviews=[{**review, "id": "R4"}]),
    ]
    fake = FakeGitHub(nodes)
    reviewed = await ContributorQueries(fake, REPO).reviewed_prs("alice", START, START + 10 * DAY)
    assert [r.pr_number for r in reviewed] == [10, 12]
    (only,) = reviewed[0].reviews
    assert (only.review_id, only.reviewer_login, only.pr_author_login) == ("R1", "alice", "bob")
    assert reviewed[1].reviews[0].pr_author_login == "ghost"
    assert fake.calls[0][1]["reviewer"] == "alice"


async def test_login_goes_only_into_variables():
    fake = FakeGitHub([])
    await ContributorQueries(fake, REPO).authored_prs("alice", START, START + DAY)
    query, variables = fake.calls[0]
    assert query == AUTHORED_QUERY
    assert "alice" not in query
    assert "author:alice" in variables["q"]


@pytest.mark.parametrize("login", ["x repo:evil/repo", "alice author:bob", "a\nb", "alice\""])
async def test_injection_payloads_never_reach_github(login):
    fake = FakeGitHub([])
    queries = ContributorQueries(fake, REPO)
    with pytest.raises(InvalidLogin):
        await queries.authored_prs(login, START, START + DAY)
    with pytest.raises(InvalidLogin):
        await queries.reviewed_prs(login, START, START + DAY)
    assert fake.calls == []
