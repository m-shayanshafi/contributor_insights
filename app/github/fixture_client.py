"""Recorded GitHub data, served through the same interface as the live client.

Layout of the fixtures directory:
  manifest.json      {"repo", "recorded_at", "history_days", "logins": [...]}
  <login>.json       {"login", "pull_requests": [...], "reviews": [...]}

In fixture mode the clock is frozen at `recorded_at`, so windows line up with the data.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from app.analytics.periods import DAY
from app.github.client import NotFound
from app.github.queries import ReviewedPR
from app.github.validation import validate_login
from app.store.records import PullRequest, Review, SyncStatus
from app.store.repository import Repository

MANIFEST = "manifest.json"


@dataclass(frozen=True)
class ContributorFixture:
    login: str
    pull_requests: list[PullRequest]
    reviews: list[Review]


class FixtureSource:
    def __init__(self, fixtures_dir: Path) -> None:
        manifest = json.loads((fixtures_dir / MANIFEST).read_text())
        self.repo: str = manifest["repo"]
        self.recorded_at: int = manifest["recorded_at"]
        self.history_days: int = manifest["history_days"]
        self._contributors: dict[str, ContributorFixture] = {}
        for login in manifest["logins"]:
            # Validated before it becomes a path, so a manifest can't point outside the directory.
            fixture = load_contributor(fixtures_dir / f"{validate_login(login)}.json")
            self._contributors[fixture.login.lower()] = fixture

    def contributors(self) -> list[ContributorFixture]:
        return list(self._contributors.values())

    def clock(self) -> float:
        return float(self.recorded_at)

    async def get_user(self, login: str) -> str:
        fixture = self._contributors.get(login.lower())
        if fixture is None:
            raise NotFound("user not in fixtures")
        return fixture.login

    async def authored_prs(self, login: str, start: int, end: int) -> list[PullRequest]:
        fixture = self._contributors.get(login.lower())
        prs = fixture.pull_requests if fixture else []
        return [pr for pr in prs if start <= pr.updated_at < end]

    async def reviewed_prs(self, login: str, start: int, end: int) -> list[ReviewedPR]:
        fixture = self._contributors.get(login.lower())
        by_pr: dict[int, list[Review]] = {}
        for review in fixture.reviews if fixture else []:
            if start <= review.submitted_at < end:
                by_pr.setdefault(review.pr_number, []).append(review)
        return [ReviewedPR(number, reviews) for number, reviews in sorted(by_pr.items())]


async def seed_store(repo: Repository, source: FixtureSource) -> None:
    """Load every fixture contributor into the store as fully synced."""
    now = source.recorded_at
    covered_since = now - source.history_days * DAY
    for fixture in source.contributors():
        if await repo.get_contributor(fixture.login) is None:
            # Dated at the start of their history, not `now`: with the clock frozen,
            # seeded contributors would otherwise fill the new-contributors-per-hour cap.
            await repo.create_contributor(fixture.login, covered_since)
        await repo.upsert_pull_requests(fixture.pull_requests)
        for reviewed in await source.reviewed_prs(fixture.login, 0, now + 1):
            await repo.replace_reviews(reviewed.pr_number, fixture.login, reviewed.reviews)
        await repo.update_contributor(
            fixture.login, status=SyncStatus.READY, progress=1.0, error=None,
            covered_since=covered_since,
            prs_watermark=now, reviews_watermark=now, last_sync_at=now,
        )


def load_contributor(path: Path) -> ContributorFixture:
    data = json.loads(path.read_text())
    return ContributorFixture(
        login=validate_login(data["login"]),
        pull_requests=[PullRequest(**pr) for pr in data["pull_requests"]],
        reviews=[Review(**review) for review in data["reviews"]],
    )


def write_contributor(path: Path, fixture: ContributorFixture) -> None:
    """One record per line keeps the files diffable and spot-checkable."""
    def rows(records: list) -> str:
        return ",\n".join(f"  {json.dumps(asdict(r), sort_keys=True)}" for r in records)

    path.write_text(
        f'{{"login": {json.dumps(fixture.login)},\n'
        f'"pull_requests": [\n{rows(fixture.pull_requests)}\n],\n'
        f'"reviews": [\n{rows(fixture.reviews)}\n]}}\n'
    )
