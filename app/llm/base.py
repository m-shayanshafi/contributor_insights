"""What the narrative service needs from an LLM."""

from typing import Protocol

from app.models import NarrativeLLMOutput


class LLMError(Exception):
    """The model failed, refused, was rate-limited or returned unusable output."""


class NarrativeLLM(Protocol):
    model: str

    async def generate(self, system: str, user: str, key: str) -> NarrativeLLMOutput:
        """`key` identifies the input (its hash); recorded clients look responses up by it."""
        ...
