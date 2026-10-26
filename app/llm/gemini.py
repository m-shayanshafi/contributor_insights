"""Gemini client: JSON output constrained to the narrative schema, validated on receipt.

Failures (API errors including free-tier 429s, safety blocks, truncation, invalid JSON)
all become LLMError. Nothing is retried in the request path; a 429 needs time to refill.
"""

import logging

from google import genai
from google.genai import errors, types
from pydantic import SecretStr, ValidationError

from app.llm.base import LLMError
from app.models import NarrativeLLMOutput

logger = logging.getLogger(__name__)

TIMEOUT_MS = 30_000
MAX_OUTPUT_TOKENS = 2048
TEMPERATURE = 0.2


class GeminiLLM:
    def __init__(self, api_key: SecretStr, model: str) -> None:
        # vertexai=False pins the public Gemini API endpoint regardless of environment variables.
        self._client = genai.Client(api_key=api_key.get_secret_value(), vertexai=False,
                                    http_options=types.HttpOptions(timeout=TIMEOUT_MS))
        self.model = model

    async def aclose(self) -> None:
        await self._client.aio.aclose()

    async def generate(self, system: str, user: str, key: str) -> NarrativeLLMOutput:
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=TEMPERATURE,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            response_mime_type="application/json",
            response_schema=NarrativeLLMOutput,
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self.model, contents=user, config=config)
        except errors.APIError as exc:
            # Log the status only; error details can echo request content.
            logger.warning("Gemini API error %s", exc.code)
            raise LLMError(f"Gemini API error {exc.code}") from None

        feedback = response.prompt_feedback
        if feedback is not None and feedback.block_reason is not None:
            raise LLMError(f"Gemini blocked the prompt: {feedback.block_reason}")
        candidate = (response.candidates or [None])[0]
        reason = candidate.finish_reason if candidate is not None else None
        if reason is not types.FinishReason.STOP:
            raise LLMError(f"Gemini stopped early: {reason}")
        try:
            return NarrativeLLMOutput.model_validate_json(response.text or "")
        except ValidationError:
            raise LLMError("Gemini returned output that doesn't match the schema") from None
