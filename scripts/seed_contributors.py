"""Backfill contributors into the live database so they can be used as peers.

Usage: uv run python -m scripts.seed_contributors [--top 10] [login ...]

With no logins, seeds the most active human authors of the last 30 days.
"""

import argparse
import asyncio
import time

from app.github.client import GitHubClient
from app.github.queries import ContributorQueries
from app.store.db import connect
from app.store.repository import Repository
from app.sync.contributor_sync import ContributorSync
from scripts.common import active_authors, live_settings


async def seed(logins: list[str], top: int) -> None:
    # The hourly new-contributor cap protects the API from callers, not the operator.
    settings = live_settings(max_new_contributors_per_hour=1_000_000)
    conn = await connect(settings.db_path)
    try:
        async with GitHubClient(settings.github_token) as client:
            if not logins:
                logins = await active_authors(client, settings.target_repo, int(time.time()), top)
            sync = ContributorSync(Repository(conn), ContributorQueries(client, settings.target_repo),
                                   client, settings)
            for login in logins:
                result = await sync.ensure_fresh(login)
                print(f"{result.state.login}: {result.state.status}")
            await sync.wait_for_backfills()
            for login in logins:
                state = (await sync.ensure_fresh(login)).state
                print(f"{state.login}: {state.status}" + (f" ({state.error})" if state.error else ""))
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logins", nargs="*")
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()
    asyncio.run(seed(args.logins, args.top))


if __name__ == "__main__":
    main()
