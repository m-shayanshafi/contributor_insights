"""Evaluation periods and their time windows. Timestamps are UTC epoch seconds."""

from dataclasses import dataclass
from enum import StrEnum

DAY = 86_400
WEEK = 7 * DAY


class Period(StrEnum):
    WEEK = "1w"
    MONTH = "1m"
    QUARTER = "3m"

    @property
    def days(self) -> int:
        return _PERIOD_DAYS[self]


_PERIOD_DAYS = {Period.WEEK: 7, Period.MONTH: 30, Period.QUARTER: 90}

# Each period is compared with the window of the same length right before it.
REQUIRED_HISTORY_DAYS = 2 * max(_PERIOD_DAYS.values())


@dataclass(frozen=True)
class Window:
    """Half-open time range: start <= t < end."""

    start: int
    end: int

    def contains(self, ts: int) -> bool:
        return self.start <= ts < self.end


def windows(period: Period, now: int) -> tuple[Window, Window]:
    """Return (current, previous) windows ending at `now`."""
    length = period.days * DAY
    current = Window(now - length, now)
    previous = Window(now - 2 * length, now - length)
    return current, previous
