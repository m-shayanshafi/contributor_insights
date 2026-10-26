"""Builds the narrative: input -> cache lookup -> LLM -> grounding -> cache store."""

import json
from dataclasses import dataclass
from datetime import date

from app.analytics.window_records import WindowRecords
from app.llm.base import NarrativeLLM
from app.models import ContributorMetrics, EvidenceItem
from app.narrative.grounding import data_sufficiency, ground
from app.narrative.prompt import SYSTEM_PROMPT, build_input, input_hash, render_user_message
from app.store.repository import Repository


@dataclass(frozen=True)
class Narrative:
    narrative: str
    self_comparison: str
    root_cause_hypothesis: str | None
    confidence: float
    llm_confidence: float
    data_sufficiency: float
    evidence_chain: list[EvidenceItem]
    ungrounded_evidence_dropped: int
    model: str
    cached: bool = False


class NarrativeService:
    def __init__(self, repo: Repository, llm: NarrativeLLM, repo_name: str) -> None:
        self._repo = repo
        self._llm = llm
        self._repo_name = repo_name

    async def narrate(self, login: str, period: str, window_from: date, window_to: date,
                      metrics: ContributorMetrics, records: WindowRecords, now: int) -> Narrative:
        """Raises LLMError when the model fails; callers still have the metrics."""
        data = build_input(self._repo_name, login, period, window_from, window_to,
                           metrics, records.current_prs)
        key = input_hash(data)
        if cached := await self._repo.get_narrative(login, period, key):
            stored = json.loads(cached)
            stored["evidence_chain"] = [EvidenceItem(**e) for e in stored["evidence_chain"]]
            return Narrative(**stored, cached=True)

        output = await self._llm.generate(SYSTEM_PROMPT, render_user_message(data), key)
        grounded = ground(output, data, data_sufficiency(records))
        result = Narrative(
            narrative=grounded.output.narrative,
            self_comparison=grounded.output.self_comparison,
            root_cause_hypothesis=grounded.output.root_cause_hypothesis,
            confidence=grounded.output.confidence,
            llm_confidence=grounded.llm_confidence,
            data_sufficiency=grounded.data_sufficiency,
            evidence_chain=grounded.output.evidence_chain,
            ungrounded_evidence_dropped=grounded.ungrounded_evidence_dropped,
            model=self._llm.model,
        )
        stored = {**result.__dict__, "evidence_chain": [e.model_dump() for e in result.evidence_chain]}
        stored.pop("cached")
        await self._repo.put_narrative(login, period, key, json.dumps(stored), now)
        return result
