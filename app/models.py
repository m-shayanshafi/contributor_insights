"""API response models."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MetricValue(BaseModel):
    value: int | float | None
    previous_period: int | float | None
    pct_change: float | None


class SeriesBucket(BaseModel):
    bucket: str  # ISO date of the bucket's first day
    prs_merged: int
    reviews_given: int


class ContributorMetrics(BaseModel):
    prs_merged: MetricValue
    lines_changed: MetricValue
    median_pr_size_lines: MetricValue
    median_cycle_time_hours: MetricValue
    reviews_given: MetricValue
    active_weeks: MetricValue
    series: list[SeriesBucket]


class WindowRange(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: date = Field(alias="from")
    to: date


class Meta(BaseModel):
    last_synced_at: datetime | None
    stale: bool = False


class ContributorResponse(BaseModel):
    repo: str
    login: str
    period: str
    window: WindowRange
    metrics: ContributorMetrics
    meta: Meta


# Narrative: the schema the LLM must fill in.

class EvidenceItem(BaseModel):
    claim: str
    ref: str = Field(description="Dotted path into the input JSON, or pr:#<number>")
    value: str = Field(description="The cited value, copied exactly from the input")


class NarrativeLLMOutput(BaseModel):
    narrative: str
    self_comparison: Literal["Faster", "Slower", "Steady"]
    root_cause_hypothesis: str | None
    confidence: float = Field(ge=0, le=1)
    evidence_chain: list[EvidenceItem]
