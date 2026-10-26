"""A contributor's stored PRs and reviews, split into the current and previous windows."""

from dataclasses import dataclass

from app.analytics.periods import Window
from app.store.records import PullRequest, Review
from app.store.repository import Repository


@dataclass(frozen=True)
class WindowRecords:
    current_prs: list[PullRequest]
    current_reviews: list[Review]
    previous_prs: list[PullRequest]
    previous_reviews: list[Review]

    @property
    def activity(self) -> int:
        return sum(map(len, (self.current_prs, self.current_reviews,
                             self.previous_prs, self.previous_reviews)))


async def load_window_records(repo: Repository, login: str,
                              current: Window, previous: Window) -> WindowRecords:
    prs = await repo.get_merged_prs(login, previous.start, current.end)
    reviews = await repo.get_reviews_given(login, previous.start, current.end)
    return WindowRecords(
        current_prs=[p for p in prs if current.contains(p.merged_at)],
        current_reviews=[r for r in reviews if current.contains(r.submitted_at)],
        previous_prs=[p for p in prs if previous.contains(p.merged_at)],
        previous_reviews=[r for r in reviews if previous.contains(r.submitted_at)],
    )
