import pytest

from app.analytics.metrics import contributor_metrics, pct_change, series, window_stats
from app.analytics.periods import DAY, WEEK, Period, Window, windows
from app.store.records import PullRequest, Review

HOUR = 3600
NOW = 1_800_000_000  # 2027-01-15, fixed so tests are deterministic


def pr(number, merged_at, created_at=None, additions=10, deletions=0):
    return PullRequest(
        number=number, author_login="alice", title="t",
        created_at=created_at if created_at is not None else merged_at - 10 * HOUR,
        updated_at=merged_at, state="MERGED", additions=additions, deletions=deletions,
        merged_at=merged_at,
    )


def review(review_id, submitted_at):
    return Review(
        review_id=review_id, pr_number=1, pr_author_login="bob",
        reviewer_login="alice", state="APPROVED", submitted_at=submitted_at,
    )


# Periods

@pytest.mark.parametrize("period, days", [(Period.WEEK, 7), (Period.MONTH, 30), (Period.QUARTER, 90)])
def test_windows_are_adjacent_and_equal_length(period, days):
    current, previous = windows(period, NOW)
    assert current == Window(NOW - days * DAY, NOW)
    assert previous == Window(NOW - 2 * days * DAY, NOW - days * DAY)
    assert previous.end == current.start


def test_window_is_half_open():
    window = Window(100, 200)
    assert window.contains(100) and window.contains(199)
    assert not window.contains(200) and not window.contains(99)


# Window stats

def test_window_stats_values():
    window = Window(0, 30 * DAY)
    prs = [
        pr(1, merged_at=1 * DAY, created_at=1 * DAY - 2 * HOUR, additions=100, deletions=20),
        pr(2, merged_at=2 * DAY, created_at=2 * DAY - 4 * HOUR, additions=10),
        pr(3, merged_at=15 * DAY, created_at=15 * DAY - 30 * HOUR, additions=300),
    ]
    reviews = [review("r1", 3 * DAY), review("r2", 22 * DAY)]
    stats = window_stats(prs, reviews, window)
    assert stats == {
        "prs_merged": 3,
        "lines_changed": 430,
        "median_pr_size_lines": 120,
        "median_cycle_time_hours": 4.0,
        "reviews_given": 2,
        "active_weeks": 3,  # weeks 0 (PRs 1-2, review 1), 2 (PR 3), 3 (review 2)
    }


def test_window_stats_empty_window():
    stats = window_stats([], [], Window(0, 7 * DAY))
    assert stats == {
        "prs_merged": 0, "lines_changed": 0, "median_pr_size_lines": None,
        "median_cycle_time_hours": None, "reviews_given": 0, "active_weeks": 0,
    }


def test_median_pr_size_rounds_to_whole_lines():
    prs = [pr(1, merged_at=DAY, additions=35), pr(2, merged_at=DAY, additions=41)]
    # Sizes 35 and 41: median 38.0 becomes 38; 35 and 42 give 38.5, rounded half up to 39.
    assert window_stats(prs, [], Window(0, 7 * DAY))["median_pr_size_lines"] == 38
    prs[1] = pr(2, merged_at=DAY, additions=42)
    size = window_stats(prs, [], Window(0, 7 * DAY))["median_pr_size_lines"]
    assert size == 39 and isinstance(size, int)


def test_reviews_only_still_count_as_active():
    stats = window_stats([], [review("r1", 0), review("r2", 8 * DAY)], Window(0, 30 * DAY))
    assert stats["active_weeks"] == 2
    assert stats["median_pr_size_lines"] is None


# Percent change

@pytest.mark.parametrize(
    "current, previous, expected",
    [
        (14, 31, -54.8),
        (420, 95, 342.1),
        (5, 5, 0.0),
        (0, 0, 0.0),      # no activity in either window: no change
        (3, 0, None),     # growth from zero has no meaningful percentage
        (0, 4, -100.0),
        (None, 10, None),
        (10, None, None),
    ],
)
def test_pct_change(current, previous, expected):
    assert pct_change(current, previous) == expected


# Series

def test_weekly_series_buckets_and_labels():
    window = Window(NOW - 30 * DAY, NOW)
    buckets = series(
        [pr(1, window.start), pr(2, window.start + WEEK - 1), pr(3, window.start + WEEK)],
        [review("r1", NOW - 1)],
        Period.MONTH, window,
    )
    assert len(buckets) == 5  # 30 days = 4 full weeks + a partial one
    assert [(b.prs_merged, b.reviews_given) for b in buckets] == [(2, 0), (1, 0), (0, 0), (0, 0), (0, 1)]
    assert buckets[0].bucket == "2026-12-16"
    assert buckets[1].bucket == "2026-12-23"


def test_daily_series_for_one_week():
    window = Window(NOW - 7 * DAY, NOW)
    buckets = series([pr(1, NOW - 1)], [], Period.WEEK, window)
    assert len(buckets) == 7
    assert [b.prs_merged for b in buckets] == [0, 0, 0, 0, 0, 0, 1]


def test_quarter_series_has_13_weekly_buckets():
    window = Window(NOW - 90 * DAY, NOW)
    assert len(series([], [], Period.QUARTER, window)) == 13


# Assembled metrics

def test_contributor_metrics_combines_windows():
    current, previous = windows(Period.MONTH, NOW)
    result = contributor_metrics(
        Period.MONTH, current, previous,
        current_prs=[pr(1, NOW - DAY, additions=50)],
        current_reviews=[review("r1", NOW - DAY), review("r2", NOW - 2 * DAY)],
        previous_prs=[pr(2, previous.start, additions=10), pr(3, previous.start + DAY, additions=30)],
        previous_reviews=[],
    )
    assert result.prs_merged.model_dump() == {"value": 1, "previous_period": 2, "pct_change": -50.0}
    assert result.median_pr_size_lines.model_dump() == {"value": 50, "previous_period": 20, "pct_change": 150.0}
    assert result.reviews_given.model_dump() == {"value": 2, "previous_period": 0, "pct_change": None}
    assert len(result.series) == 5
