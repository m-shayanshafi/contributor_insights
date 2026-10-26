import json

import pytest
from google.genai import errors, types
from pydantic import SecretStr

from app.llm.base import LLMError
from app.llm.gemini import GeminiLLM
from app.llm.recorded import RecordedLLM, RecordingLLM

VALID = json.dumps({"narrative": "n", "self_comparison": "Steady", "root_cause_hypothesis": None,
                    "confidence": 0.5, "evidence_chain": []})
KEY = "a" * 64


def response(text=VALID, finish=types.FinishReason.STOP, blocked=None):
    feedback = types.GenerateContentResponsePromptFeedback(block_reason=blocked) if blocked else None
    candidates = [] if blocked else [types.Candidate(
        content=types.Content(role="model", parts=[types.Part(text=text)]), finish_reason=finish)]
    return types.GenerateContentResponse(candidates=candidates, prompt_feedback=feedback)


@pytest.fixture
def gemini(monkeypatch):
    llm = GeminiLLM(SecretStr("AIzaTESTONLY0123456789abcdefghijklmnopq"), "gemini-test")
    llm.calls = []
    llm.reply = response()

    async def fake_generate(*, model, contents, config):
        llm.calls.append((model, contents, config))
        if isinstance(llm.reply, Exception):
            raise llm.reply
        return llm.reply

    monkeypatch.setattr(llm._client.aio.models, "generate_content", fake_generate)
    return llm


async def test_gemini_request_and_parse(gemini):
    output = await gemini.generate("system", "<data>{}</data>", KEY)
    assert output.self_comparison == "Steady"
    model, contents, config = gemini.calls[0]
    assert (model, contents) == ("gemini-test", "<data>{}</data>")
    assert config.system_instruction == "system"
    assert config.response_mime_type == "application/json"
    assert config.temperature == 0.2 and config.max_output_tokens == 2048


@pytest.mark.parametrize("reply, message", [
    (errors.ClientError(429, {"error": {"message": "quota for project X"}}), "Gemini API error 429"),
    (errors.ServerError(503, {"error": {"message": "overloaded"}}), "Gemini API error 503"),
    (response(finish=types.FinishReason.MAX_TOKENS), "stopped early"),
    (response(finish=types.FinishReason.SAFETY), "stopped early"),
    (response(blocked=types.BlockedReason.SAFETY), "blocked the prompt"),
    (response(text='{"narrative": "missing fields"}'), "doesn't match the schema"),
    (response(text="not json"), "doesn't match the schema"),
])
async def test_gemini_failures_become_llm_errors(gemini, reply, message):
    gemini.reply = reply
    with pytest.raises(LLMError, match=message) as exc_info:
        await gemini.generate("system", "user", KEY)
    assert "quota for project" not in str(exc_info.value)


async def test_recording_then_replay(tmp_path, gemini):
    recorded = await RecordingLLM(gemini, tmp_path).generate("s", "u", KEY)
    assert (tmp_path / f"{KEY}.json").exists()
    assert await RecordedLLM(tmp_path).generate("s", "u", KEY) == recorded


async def test_recorded_missing_or_bad_key(tmp_path):
    with pytest.raises(LLMError, match="no recorded narrative"):
        await RecordedLLM(tmp_path).generate("s", "u", KEY)
    with pytest.raises(LLMError, match="invalid recording key"):
        await RecordedLLM(tmp_path).generate("s", "u", "../../etc/passwd")
