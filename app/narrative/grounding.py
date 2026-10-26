"""Post-processing of the LLM output: evidence must match the input, confidence is capped.

Refs are resolved by walking dicts and lists only; nothing is evaluated.
"""

import re
from dataclasses import dataclass

from app.analytics.window_records import WindowRecords
from app.models import EvidenceItem, NarrativeLLMOutput

PR_REF = re.compile(r"^pr:#(\d+)$")
NUMBER_TOLERANCE = 0.051  # values are rounded to one decimal
FULL_CONFIDENCE_ACTIVITY = 20
_MISSING = object()


@dataclass(frozen=True)
class GroundedNarrative:
    output: NarrativeLLMOutput
    ungrounded_evidence_dropped: int
    llm_confidence: float
    data_sufficiency: float


def data_sufficiency(records: WindowRecords) -> float:
    """1.0 at 20+ PRs and reviews across both windows, scaling down linearly below that."""
    return round(min(1.0, records.activity / FULL_CONFIDENCE_ACTIVITY), 2)


def ground(output: NarrativeLLMOutput, data: dict, sufficiency: float) -> GroundedNarrative:
    kept = [item for item in output.evidence_chain if _is_grounded(item, data)]
    changes: dict = {
        "evidence_chain": kept,
        "confidence": round(min(output.confidence, sufficiency), 2),
    }
    if not kept:
        changes["root_cause_hypothesis"] = None
    return GroundedNarrative(
        output=output.model_copy(update=changes),
        ungrounded_evidence_dropped=len(output.evidence_chain) - len(kept),
        llm_confidence=output.confidence,
        data_sufficiency=sufficiency,
    )


def _is_grounded(item: EvidenceItem, data: dict) -> bool:
    ref = item.ref.strip()
    if match := PR_REF.match(ref):
        pr = next((p for p in data["top_prs"] if p["number"] == int(match[1])), None)
        # A PR ref may cite any of that PR's fields, e.g. its size or its number.
        return pr is not None and any(_values_match(item.value, v) for v in pr.values())
    actual = resolve(data, ref)
    if actual is _MISSING or isinstance(actual, dict | list):
        return False
    return _values_match(item.value, actual)


def resolve(data: object, path: str) -> object:
    """Follow a dotted path such as metrics.prs_merged.value or series.2.prs_merged."""
    node = data
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _MISSING
    return node


def _values_match(claimed: str, actual: object) -> bool:
    claimed = claimed.strip()
    if actual is None:
        return claimed.lower() in {"null", "none"}
    if isinstance(actual, int | float):
        try:
            number = float(claimed.replace(",", "").removesuffix("%").removeprefix("+"))
        except ValueError:
            return False
        return abs(number - actual) <= NUMBER_TOLERANCE
    return claimed.casefold() == str(actual).strip().casefold()
