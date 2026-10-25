"""Contributor velocity metrics. Pure functions over stored records."""

from datetime import UTC, datetime
from statistics import median

from app.analytics.periods import DAY, WEEK, Period, Window
from app.models import ContributorMetrics, MetricValue, SeriesBucket
from app.store.records import PullRequest, Review

Number = int | float | None


def window_stats(prs: list[PullRequest], reviews: list[Review], window: Window) -> dict[str, Number]:
    """The six metrics for one window.

    `prs` are the login's PRs merged in the window, `reviews` the reviews they gave
    on other people's PRs in the window (the repository already excludes self-reviews).
    """
    sizes = [pr.lines_changed for pr in prs]
    cycle_hours = [(pr.merged_at - pr.created_at) / 3600 for pr in prs]
    active = {(pr.merged_at - window.start) // WEEK for pr in prs}
    active |= {(r.submitted_at - window.start) // WEEK for r in reviews}
    return {
        "prs_merged": len(prs),
        "lines_changed": sum(sizes),
        # Medians are None without PRs: 0 would wrongly suggest empty or instant PRs.
        "median_pr_size_lines": median(sizes) if sizes else None,
        "median_cycle_time_hours": round(median(cycle_hours), 1) if cycle_hours else None,
        "reviews_given": len(reviews),
        "active_weeks": len(active),
    }


def pct_change(current: Number, previous: Number) -> float | None:
    """Percent change from previous to current, or None when undefined."""
    if current is None or previous is None:
        return None
    if previous == 0:
        # 0 -> 0 is no change; 0 -> N has no meaningful percentage.
        return 0.0 if current == 0 else None
    return round((current - previous) / previous * 100, 1)


def series(prs: list[PullRequest], reviews: list[Review], period: Period,
           window: Window) -> list[SeriesBucket]:
    """Trend buckets: daily for 1w, weekly for 1m and 3m, aligned to the window start."""
    size = DAY if period is Period.WEEK else WEEK
    count = -(-(window.end - window.start) // size)  # ceiling division
    prs_per_bucket = [0] * count
    reviews_per_bucket = [0] * count
    for pr in prs:
        prs_per_bucket[(pr.merged_at - window.start) // size] += 1
    for review in reviews:
        reviews_per_bucket[(review.submitted_at - window.start) // size] += 1
    return [
        SeriesBucket(
            bucket=datetime.fromtimestamp(window.start + i * size, UTC).date().isoformat(),
            prs_merged=prs_per_bucket[i],
            reviews_given=reviews_per_bucket[i],
        )
        for i in range(count)
    ]


def contributor_metrics(
    period: Period,
    current: Window,
    previous: Window,
    current_prs: list[PullRequest],
    current_reviews: list[Review],
    previous_prs: list[PullRequest],
    previous_reviews: list[Review],
) -> ContributorMetrics:
    now_stats = window_stats(current_prs, current_reviews, current)
    before_stats = window_stats(previous_prs, previous_reviews, previous)
    values = {
        name: MetricValue(
            value=now_stats[name],
            previous_period=before_stats[name],
            pct_change=pct_change(now_stats[name], before_stats[name]),
        )
        for name in now_stats
    }
    return ContributorMetrics(
        **values, series=series(current_prs, current_reviews, period, current)
    )
