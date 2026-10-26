"""Recorded Gemini responses for credential-free fixture mode, keyed by input hash."""

import json
from pathlib import Path

from app.llm.base import LLMError, NarrativeLLM
from app.models import NarrativeLLMOutput

HASH_LENGTH = 64


class RecordedLLM:
    model = "recorded"

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    async def generate(self, system: str, user: str, key: str) -> NarrativeLLMOutput:
        if len(key) != HASH_LENGTH or not all(c in "0123456789abcdef" for c in key):
            raise LLMError("invalid recording key")
        path = self._directory / f"{key}.json"
        if not path.exists():
            raise LLMError("no recorded narrative for this input")
        return NarrativeLLMOutput.model_validate_json(path.read_text())


class RecordingLLM:
    """Wraps a live client and saves each raw response for RecordedLLM to replay.

    Inputs already recorded are replayed without an API call, so an interrupted
    recording run (e.g. a free-tier quota error) can simply be restarted.
    """

    def __init__(self, inner: NarrativeLLM, directory: Path) -> None:
        self._inner = inner
        self._directory = directory
        self._replay = RecordedLLM(directory)
        self.model = inner.model

    async def generate(self, system: str, user: str, key: str) -> NarrativeLLMOutput:
        if (self._directory / f"{key}.json").exists():
            return await self._replay.generate(system, user, key)
        output = await self._inner.generate(system, user, key)
        self._directory.mkdir(parents=True, exist_ok=True)
        (self._directory / f"{key}.json").write_text(
            json.dumps(output.model_dump(), indent=2) + "\n")
        return output
