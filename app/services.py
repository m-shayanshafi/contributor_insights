"""Long-lived components shared by the routes, built once per app."""

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from app.config import DataSource, LLMMode, Settings
from app.github.client import GitHubClient
from app.github.fixture_client import FixtureSource, seed_store
from app.github.queries import ContributorQueries
from app.llm.base import NarrativeLLM
from app.llm.gemini import GeminiLLM
from app.llm.recorded import RecordedLLM
from app.narrative.service import NarrativeService
from app.store.db import connect
from app.store.repository import Repository
from app.sync.contributor_sync import ContributorSync


@dataclass
class Services:
    repo: Repository
    sync: ContributorSync
    narratives: NarrativeService
    clock: Callable[[], float] = time.time


@asynccontextmanager
async def open_services(settings: Settings) -> AsyncIterator[Services]:
    conn = await connect(settings.db_path)
    repo = Repository(conn)
    client = sync = llm = None
    try:
        llm = build_llm(settings)
        narratives = NarrativeService(repo, llm, settings.target_repo)
        if settings.data_source is DataSource.GITHUB:
            client = GitHubClient(settings.github_token)
            queries = ContributorQueries(client, settings.target_repo)
            sync = ContributorSync(repo, queries, client, settings)
            services = Services(repo=repo, sync=sync, narratives=narratives)
        else:
            source = FixtureSource(settings.fixtures_dir)
            if source.repo != settings.target_repo:
                raise RuntimeError(f"Fixtures were recorded for {source.repo}, "
                                   f"not TARGET_REPO={settings.target_repo}")
            await seed_store(repo, source)
            # Same sync code path as live mode, with the clock frozen at recording time.
            sync = ContributorSync(repo, source, source, settings, clock=source.clock)
            services = Services(repo=repo, sync=sync, narratives=narratives, clock=source.clock)
        yield services
    finally:
        if sync is not None:
            await sync.aclose()
        if client is not None:
            await client.aclose()
        if isinstance(llm, GeminiLLM):
            await llm.aclose()
        await conn.close()


def build_llm(settings: Settings) -> NarrativeLLM:
    if settings.llm_mode is LLMMode.LIVE:
        return GeminiLLM(settings.gemini_api_key, settings.gemini_model)
    return RecordedLLM(settings.fixtures_dir / "narratives")
