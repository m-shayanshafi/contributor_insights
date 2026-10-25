"""Plain records shared by the store, the GitHub client and the metrics engine.

All timestamps are UTC epoch seconds.
"""

from dataclasses import dataclass
from enum import StrEnum


class SyncStatus(StrEnum):
    FETCHING = "fetching"
    READY = "ready"
    ERROR = "error"


@dataclass(frozen=True)
class PullRequest:
    number: int
    author_login: str
    title: str
    created_at: int
    updated_at: int
    state: str
    additions: int
    deletions: int
    merged_at: int | None = None
    closed_at: int | None = None

    @property
    def lines_changed(self) -> int:
        return self.additions + self.deletions


@dataclass(frozen=True)
class Review:
    review_id: str
    pr_number: int
    pr_author_login: str
    reviewer_login: str
    state: str
    submitted_at: int


@dataclass(frozen=True)
class ContributorState:
    login: str
    status: SyncStatus
    progress: float
    created_at: int
    covered_since: int | None = None
    prs_watermark: int | None = None
    reviews_watermark: int | None = None
    last_sync_at: int | None = None
    error: str | None = None
