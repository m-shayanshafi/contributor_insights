"""Data access layer. All SQL uses bound parameters.

Time windows are half-open: start <= t < end.
"""

from dataclasses import fields

import aiosqlite

from app.store.records import ContributorState, PullRequest, Review, SyncStatus

# Columns that update_contributor() may set. Column names are never taken from input.
_UPDATABLE = frozenset(
    {"status", "progress", "covered_since", "prs_watermark", "reviews_watermark",
     "last_sync_at", "error"}
)
_PR_COLUMNS = [f.name for f in fields(PullRequest)]
_REVIEW_COLUMNS = [f.name for f in fields(Review)]


class Repository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    # Contributors

    async def get_contributor(self, login: str) -> ContributorState | None:
        cursor = await self._conn.execute("SELECT * FROM contributors WHERE login = ?", (login,))
        row = await cursor.fetchone()
        if row is None:
            return None
        values = dict(row)
        values["status"] = SyncStatus(values["status"])
        return ContributorState(**values)

    async def create_contributor(self, login: str, now: int) -> None:
        await self._conn.execute(
            "INSERT INTO contributors (login, status, progress, created_at) VALUES (?, ?, 0, ?)",
            (login, SyncStatus.FETCHING, now),
        )
        await self._conn.commit()

    async def update_contributor(self, login: str, /, **changes: object) -> None:
        unknown = set(changes) - _UPDATABLE
        if unknown:
            raise ValueError(f"Cannot update contributor columns: {sorted(unknown)}")
        if not changes:
            return
        assignments = ", ".join(f"{column} = ?" for column in changes)
        await self._conn.execute(
            f"UPDATE contributors SET {assignments} WHERE login = ?",
            (*changes.values(), login),
        )
        await self._conn.commit()

    async def count_contributors_created_since(self, since: int) -> int:
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM contributors WHERE created_at >= ?", (since,)
        )
        (count,) = await cursor.fetchone()
        return count

    # Pull requests and reviews

    async def upsert_pull_requests(self, prs: list[PullRequest]) -> None:
        columns = ", ".join(_PR_COLUMNS)
        placeholders = ", ".join("?" for _ in _PR_COLUMNS)
        updates = ", ".join(f"{c} = excluded.{c}" for c in _PR_COLUMNS if c != "number")
        await self._conn.executemany(
            f"INSERT INTO pull_requests ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT (number) DO UPDATE SET {updates}",
            [tuple(getattr(pr, c) for c in _PR_COLUMNS) for pr in prs],
        )
        await self._conn.commit()

    async def replace_reviews(self, pr_number: int, reviewer_login: str, reviews: list[Review]) -> None:
        """Replace one reviewer's reviews on one PR, leaving other reviewers' rows intact."""
        columns = ", ".join(_REVIEW_COLUMNS)
        placeholders = ", ".join("?" for _ in _REVIEW_COLUMNS)
        await self._conn.execute(
            "DELETE FROM reviews WHERE pr_number = ? AND reviewer_login = ?",
            (pr_number, reviewer_login),
        )
        await self._conn.executemany(
            f"INSERT OR REPLACE INTO reviews ({columns}) VALUES ({placeholders})",
            [tuple(getattr(r, c) for c in _REVIEW_COLUMNS) for r in reviews],
        )
        await self._conn.commit()

    async def get_merged_prs(self, author_login: str, start: int, end: int) -> list[PullRequest]:
        cursor = await self._conn.execute(
            f"SELECT {', '.join(_PR_COLUMNS)} FROM pull_requests "
            "WHERE author_login = ? AND merged_at >= ? AND merged_at < ? ORDER BY merged_at",
            (author_login, start, end),
        )
        return [PullRequest(**dict(row)) for row in await cursor.fetchall()]

    async def get_reviews_given(self, reviewer_login: str, start: int, end: int) -> list[Review]:
        """Reviews submitted by the login on other people's PRs (self-reviews excluded)."""
        cursor = await self._conn.execute(
            f"SELECT {', '.join(_REVIEW_COLUMNS)} FROM reviews "
            "WHERE reviewer_login = ? AND pr_author_login != reviewer_login "
            "AND submitted_at >= ? AND submitted_at < ? ORDER BY submitted_at",
            (reviewer_login, start, end),
        )
        return [Review(**dict(row)) for row in await cursor.fetchall()]

    async def delete_older_than(self, cutoff: int) -> None:
        """Retention: drop data that can no longer fall inside any window."""
        # Keep merged PRs by merge time: a PR created before the cutoff but
        # merged after it still counts toward a window.
        await self._conn.execute(
            "DELETE FROM pull_requests WHERE (merged_at IS NOT NULL AND merged_at < ?) "
            "OR (merged_at IS NULL AND updated_at < ?)",
            (cutoff, cutoff),
        )
        await self._conn.execute("DELETE FROM reviews WHERE submitted_at < ?", (cutoff,))
        await self._conn.commit()

    # Narrative cache

    async def get_narrative(self, login: str, period: str, data_hash: str) -> str | None:
        cursor = await self._conn.execute(
            "SELECT response_json FROM narratives WHERE login = ? AND period = ? AND data_hash = ?",
            (login, period, data_hash),
        )
        row = await cursor.fetchone()
        return row["response_json"] if row else None

    async def put_narrative(self, login: str, period: str, data_hash: str,
                            response_json: str, now: int) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO narratives (login, period, data_hash, response_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (login, period, data_hash, response_json, now),
        )
        await self._conn.commit()
