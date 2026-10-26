"""Long-lived components shared by the routes, built once per app."""

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from app.config import DataSource, Settings
from app.github.client import GitHubClient
from app.github.queries import ContributorQueries
from app.store.db import connect
from app.store.repository import Repository
from app.sync.contributor_sync import ContributorSync


@dataclass
class Services:
    repo: Repository
    # None until a data source is available (fixture mode arrives in step 8).
    sync: ContributorSync | None
    clock: Callable[[], float] = time.time


@asynccontextmanager
async def open_services(settings: Settings) -> AsyncIterator[Services]:
    conn = await connect(settings.db_path)
    repo = Repository(conn)
    client = sync = None
    if settings.data_source is DataSource.GITHUB:
        client = GitHubClient(settings.github_token)
        queries = ContributorQueries(client, settings.target_repo)
        sync = ContributorSync(repo, queries, client, settings)
    try:
        yield Services(repo=repo, sync=sync)
    finally:
        if sync is not None:
            await sync.aclose()
        if client is not None:
            await client.aclose()
        await conn.close()
