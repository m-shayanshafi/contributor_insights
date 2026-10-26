"""Contributor-scoped GitHub searches: PRs authored and reviews given.

Search returns at most 1,000 results per query, so date ranges are cut into
monthly slices, and any slice that still reports more than the cap is split in
half until it fits. Every search filters on `updated:` so a PR created before the
range but merged or reviewed inside it is still found.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.analytics.periods import DAY
from app.github.client import RATE_LIMIT_FIELDS, GitHubError
from app.github.validation import validate_login
from app.store.records import PullRequest, Review

SLICE_SECONDS = 30 * DAY
MIN_SLICE_SECONDS = 60

_PR_FIELDS = """
  number title state additions deletions
  createdAt updatedAt mergedAt closedAt
  author { login }
"""

AUTHORED_QUERY = f"""
query($q: String!, $first: Int!, $cursor: String) {{
  search(query: $q, type: ISSUE, first: $first, after: $cursor) {{
    issueCount
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ ... on PullRequest {{ {_PR_FIELDS} }} }}
  }}
  {RATE_LIMIT_FIELDS}
}}
"""

REVIEWED_QUERY = f"""
query($q: String!, $first: Int!, $cursor: String, $reviewer: String!) {{
  search(query: $q, type: ISSUE, first: $first, after: $cursor) {{
    issueCount
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ ... on PullRequest {{
      number author {{ login }}
      reviews(first: 100, author: $reviewer) {{ nodes {{ id state submittedAt }} }}
    }} }}
  }}
  {RATE_LIMIT_FIELDS}
}}
"""


class GraphQLClient(Protocol):
    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ReviewedPR:
    """One reviewer's reviews on one PR; the store replaces them as a unit."""

    pr_number: int
    reviews: list[Review]


def monthly_slices(start: int, end: int) -> Iterator[tuple[int, int]]:
    """Cut [start, end) into consecutive slices of at most SLICE_SECONDS."""
    while start < end:
        yield start, min(start + SLICE_SECONDS, end)
        start += SLICE_SECONDS


def search_string(repo: str, kind: str, login: str, start: int, end: int) -> str:
    """Build the search. `login` must already be validated; it is never trusted raw."""
    qualifier = {"authored": "author", "reviewed": "reviewed-by"}[kind]
    # Search ranges are inclusive at both ends; stop one second early so slices don't overlap.
    return (
        f"repo:{repo} is:pr {qualifier}:{login} "
        f"updated:{_iso(start)}..{_iso(end - 1)} sort:updated-asc"
    )


class ContributorQueries:
    def __init__(self, client: GraphQLClient, repo: str, *,
                 page_size: int = 100, result_cap: int = 1000) -> None:
        self._client = client
        self._repo = repo
        self._page_size = page_size
        self._result_cap = result_cap

    async def authored_prs(self, login: str, start: int, end: int) -> list[PullRequest]:
        """PRs authored by `login` and updated in [start, end)."""
        login = validate_login(login)
        nodes = await self._search(AUTHORED_QUERY, "authored", login, start, end, {})
        prs = {pr.number: pr for pr in map(parse_pull_request, nodes)}
        return sorted(prs.values(), key=lambda pr: pr.number)

    async def reviewed_prs(self, login: str, start: int, end: int) -> list[ReviewedPR]:
        """`login`'s reviews on other people's PRs updated in [start, end)."""
        login = validate_login(login)
        nodes = await self._search(REVIEWED_QUERY, "reviewed", login, start, end,
                                   {"reviewer": login})
        reviewed: dict[int, ReviewedPR] = {}
        for node in nodes:
            author = _login(node)
            if author.lower() == login.lower():
                continue  # self-reviews don't count as reviews given
            reviewed[node["number"]] = ReviewedPR(node["number"], parse_reviews(node, login))
        return sorted(reviewed.values(), key=lambda r: r.pr_number)

    async def _search(self, query: str, kind: str, login: str, start: int, end: int,
                      extra: dict[str, Any]) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        for slice_start, slice_end in monthly_slices(start, end):
            nodes.extend(await self._search_slice(query, kind, login, slice_start, slice_end, extra))
        return nodes

    async def _search_slice(self, query: str, kind: str, login: str, start: int, end: int,
                            extra: dict[str, Any]) -> list[dict[str, Any]]:
        variables = {"q": search_string(self._repo, kind, login, start, end),
                     "first": self._page_size, "cursor": None, **extra}
        page = (await self._client.graphql(query, variables))["search"]

        if page["issueCount"] > self._result_cap:
            if end - start <= MIN_SLICE_SECONDS:
                raise GitHubError("too many search results in a one-minute slice")
            middle = (start + end) // 2
            return (await self._search_slice(query, kind, login, start, middle, extra)
                    + await self._search_slice(query, kind, login, middle, end, extra))

        nodes = list(page["nodes"])
        while page["pageInfo"]["hasNextPage"]:
            variables["cursor"] = page["pageInfo"]["endCursor"]
            page = (await self._client.graphql(query, variables))["search"]
            nodes.extend(page["nodes"])
        # Non-PR nodes come back as empty objects from the inline fragment.
        return [node for node in nodes if node]


def parse_pull_request(node: dict[str, Any]) -> PullRequest:
    # Only the fields above are selected; author emails are never requested.
    return PullRequest(
        number=node["number"],
        author_login=_login(node),
        title=node["title"],
        created_at=_epoch(node["createdAt"]),
        updated_at=_epoch(node["updatedAt"]),
        state=node["state"],
        additions=node["additions"],
        deletions=node["deletions"],
        merged_at=_epoch(node["mergedAt"]) if node["mergedAt"] else None,
        closed_at=_epoch(node["closedAt"]) if node["closedAt"] else None,
    )


def parse_reviews(node: dict[str, Any], reviewer: str) -> list[Review]:
    return [
        Review(
            review_id=review["id"],
            pr_number=node["number"],
            pr_author_login=_login(node),
            reviewer_login=reviewer,
            state=review["state"],
            submitted_at=_epoch(review["submittedAt"]),
        )
        for review in node["reviews"]["nodes"]
        # Pending reviews are drafts nobody else can see yet.
        if review["submittedAt"] and review["state"] != "PENDING"
    ]


def _login(node: dict[str, Any]) -> str:
    # Deleted accounts come back as a null author.
    return (node.get("author") or {}).get("login") or "ghost"


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp())
