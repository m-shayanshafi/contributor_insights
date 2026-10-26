"""Helpers shared by the live-mode scripts."""

import sys
from collections import Counter
from datetime import UTC, datetime

from app.analytics.periods import DAY
from app.config import Settings
from app.github.client import RATE_LIMIT_FIELDS, GitHubClient

ACTIVE_AUTHORS_QUERY = f"""
query($q: String!, $cursor: String) {{
  search(query: $q, type: ISSUE, first: 100, after: $cursor) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ ... on PullRequest {{ author {{ __typename login }} }} }}
  }}
  {RATE_LIMIT_FIELDS}
}}
"""


def live_settings(**overrides) -> Settings:
    settings = Settings(**overrides)
    if settings.github_token is None:
        sys.exit("GITHUB_TOKEN must be set in .env to run this script")
    return settings


async def active_authors(client: GitHubClient, repo: str, now: int, top: int,
                         days: int = 30) -> list[str]:
    """The `top` human authors with the most merged PRs in the last `days` days."""
    counts: Counter[str] = Counter()
    # Weekly slices keep each search under the 1,000-result cap for a busy repo.
    for start in range(now - days * DAY, now, 7 * DAY):
        end = min(start + 7 * DAY, now)
        q = f"repo:{repo} is:pr is:merged merged:{_iso(start)}..{_iso(end - 1)}"
        cursor = None
        while True:
            page = (await client.graphql(ACTIVE_AUTHORS_QUERY, {"q": q, "cursor": cursor}))["search"]
            for node in page["nodes"]:
                author = node.get("author") or {}
                if author.get("__typename") == "User":
                    counts[author["login"]] += 1
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
    return [login for login, _ in counts.most_common(top)]


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
