"""Keeps one contributor's stored data fresh.

- A new login starts a background backfill of HISTORY_DAYS; callers see `fetching`
  until it finishes.
- A ready login synced within SYNC_TTL is served from the store without GitHub calls.
- A stale login gets a quick incremental sync of everything updated since its watermark.
  If GitHub fails, the cached data is served and marked stale.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from app.analytics.periods import DAY
from app.config import Settings
from app.github.client import GitHubError
from app.github.queries import ContributorQueries, monthly_slices
from app.github.validation import validate_login
from app.store.records import ContributorState, SyncStatus
from app.store.repository import Repository

logger = logging.getLogger(__name__)

NEW_CONTRIBUTOR_WINDOW = 3600


class UserLookup(Protocol):
    async def get_user(self, login: str) -> str: ...


class NewContributorCapReached(Exception):
    pass


@dataclass(frozen=True)
class SyncResult:
    state: ContributorState
    stale: bool = False


class ContributorSync:
    def __init__(self, repo: Repository, queries: ContributorQueries, users: UserLookup,
                 settings: Settings, clock: Callable[[], float] = time.time) -> None:
        self._repo = repo
        self._queries = queries
        self._users = users
        self._settings = settings
        self._clock = clock
        self._locks: dict[str, asyncio.Lock] = {}
        self._backfills: dict[str, asyncio.Task[None]] = {}
        self._backfill_slots = asyncio.Semaphore(settings.max_concurrent_backfills)

    async def ensure_fresh(self, login: str) -> SyncResult:
        login = validate_login(login)
        key = login.lower()
        async with self._locks.setdefault(key, asyncio.Lock()):
            state = await self._repo.get_contributor(login)
            if state is None:
                return SyncResult(await self._register(login))
            if state.status is not SyncStatus.READY:
                # Fetching with no live task (e.g. after a restart) or a failed backfill.
                if key not in self._backfills:
                    await self._repo.update_contributor(state.login, status=SyncStatus.FETCHING,
                                                        progress=0.0, error=None)
                    self._start_backfill(state.login)
                    state = await self._repo.get_contributor(login)
                return SyncResult(state)
            if self._now() - (state.last_sync_at or 0) < self._settings.sync_ttl_seconds:
                return SyncResult(state)
            return await self._incremental(state)

    async def wait_for_backfills(self) -> None:
        """Wait for running backfills to finish (used by tests and scripts)."""
        while self._backfills:
            await asyncio.gather(*self._backfills.values(), return_exceptions=True)

    async def aclose(self) -> None:
        for task in self._backfills.values():
            task.cancel()
        await asyncio.gather(*self._backfills.values(), return_exceptions=True)

    async def _register(self, login: str) -> ContributorState:
        now = self._now()
        recent = await self._repo.count_contributors_created_since(now - NEW_CONTRIBUTOR_WINDOW)
        if recent >= self._settings.max_new_contributors_per_hour:
            raise NewContributorCapReached()
        # Raises NotFound for unknown users, orgs and bots before anything is stored.
        canonical = await self._users.get_user(login)
        await self._repo.create_contributor(canonical, now)
        self._start_backfill(canonical)
        return await self._repo.get_contributor(canonical)

    def _start_backfill(self, login: str) -> None:
        key = login.lower()
        task = asyncio.create_task(self._backfill(login), name=f"backfill:{key}")
        self._backfills[key] = task
        task.add_done_callback(lambda _: self._backfills.pop(key, None))

    async def _backfill(self, login: str) -> None:
        async with self._backfill_slots:
            now = self._now()
            since = now - self._settings.history_days * DAY
            slices = list(monthly_slices(since, now))
            steps, done = 2 * len(slices), 0
            try:
                for start, end in slices:
                    await self._repo.upsert_pull_requests(
                        await self._queries.authored_prs(login, start, end))
                    done += 1
                    await self._repo.update_contributor(login, progress=round(done / steps, 2))
                for start, end in slices:
                    await self._store_reviews(login, start, end)
                    done += 1
                    await self._repo.update_contributor(login, progress=round(done / steps, 2))
            except Exception as exc:
                logger.warning("Backfill failed for %s: %s", login, _describe(exc),
                               exc_info=not isinstance(exc, GitHubError))
                await self._repo.update_contributor(login, status=SyncStatus.ERROR,
                                                    error=_describe(exc))
                return
            await self._repo.update_contributor(
                login, status=SyncStatus.READY, progress=1.0, error=None, covered_since=since,
                prs_watermark=now, reviews_watermark=now, last_sync_at=now,
            )
            await self._repo.delete_older_than(since)
            logger.info("Backfill finished for %s", login)

    async def _incremental(self, state: ContributorState) -> SyncResult:
        now = self._now()
        login = state.login
        try:
            await self._repo.upsert_pull_requests(
                await self._queries.authored_prs(login, state.prs_watermark, now))
            await self._store_reviews(login, state.reviews_watermark, now)
        except GitHubError as exc:
            logger.warning("Incremental sync failed for %s, serving cached data: %s",
                           login, _describe(exc))
            return SyncResult(state, stale=True)
        await self._repo.update_contributor(login, prs_watermark=now, reviews_watermark=now,
                                            last_sync_at=now)
        await self._repo.delete_older_than(now - self._settings.history_days * DAY)
        return SyncResult(await self._repo.get_contributor(login))

    async def _store_reviews(self, login: str, start: int, end: int) -> None:
        for reviewed in await self._queries.reviewed_prs(login, start, end):
            await self._repo.replace_reviews(reviewed.pr_number, login, reviewed.reviews)

    def _now(self) -> int:
        return int(self._clock())


def _describe(exc: Exception) -> str:
    # GitHubError messages are written by our client and contain no secrets or upstream bodies.
    return str(exc) if isinstance(exc, GitHubError) else "internal error"
