import pytest

from app.config import Settings

_ENV_VARS = (
    "DATA_SOURCE", "LLM_MODE", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "TARGET_REPO", "DB_PATH",
)


@pytest.fixture
def make_settings(monkeypatch, tmp_path):
    """Build Settings isolated from the developer's real environment and `.env`."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    def _make(**overrides) -> Settings:
        values = {
            "data_source": "fixtures",
            "llm_mode": "recorded",
            "db_path": tmp_path / "test.db",
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return _make
