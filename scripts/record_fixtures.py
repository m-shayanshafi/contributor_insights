"""Record live GitHub data into fixtures/ for credential-free fixture mode.

Usage: uv run python -m scripts.record_fixtures [--top 25] [login ...]

With no logins, records the most active human authors of the last 30 days.
"""

import argparse
import asyncio
import json
import re
import time
from dataclasses import replace
from pathlib import Path

from app.analytics.periods import DAY
from app.github.client import GitHubClient
from app.github.fixture_client import MANIFEST, ContributorFixture, write_contributor
from app.github.queries import ContributorQueries
from scripts.common import active_authors, live_settings

# Emails are never requested, but a PR title could still contain one.
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def scrub(fixture: ContributorFixture) -> ContributorFixture:
    prs = [replace(pr, title=EMAIL_PATTERN.sub("[email]", pr.title)) for pr in fixture.pull_requests]
    return replace(fixture, pull_requests=prs)


async def record(out_dir: Path, logins: list[str], top: int) -> None:
    settings = live_settings()
    now = int(time.time())
    since = now - settings.history_days * DAY
    out_dir.mkdir(parents=True, exist_ok=True)

    async with GitHubClient(settings.github_token) as client:
        queries = ContributorQueries(client, settings.target_repo)
        if not logins:
            logins = await active_authors(client, settings.target_repo, now, top)
        recorded = []
        for requested in logins:
            login = await client.get_user(requested)
            prs = await queries.authored_prs(login, since, now)
            reviews = [r for reviewed in await queries.reviewed_prs(login, since, now)
                       for r in reviewed.reviews]
            write_contributor(out_dir / f"{login}.json",
                              scrub(ContributorFixture(login, prs, reviews)))
            recorded.append(login)
            print(f"{login}: {len(prs)} PRs, {len(reviews)} reviews")

    manifest = {"repo": settings.target_repo, "recorded_at": now,
                "history_days": settings.history_days, "logins": recorded}
    (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logins", nargs="*")
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--out", type=Path, default=Path("fixtures"))
    args = parser.parse_args()
    asyncio.run(record(args.out, args.logins, args.top))


if __name__ == "__main__":
    main()
