import pytest
from pydantic import ValidationError

GH_TOKEN = "github_pat_TESTONLY1234567890"
ANTHROPIC_KEY = "sk-ant-TESTONLY-abcdef"


def test_fixture_mode_needs_no_secrets(make_settings):
    settings = make_settings()
    assert settings.github_token is None
    assert settings.anthropic_api_key is None


def test_github_mode_requires_token(make_settings):
    with pytest.raises(ValidationError, match="GITHUB_TOKEN is required"):
        make_settings(data_source="github")


def test_live_llm_requires_key(make_settings):
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY is required"):
        make_settings(llm_mode="live")


def test_blank_token_counts_as_missing(make_settings):
    with pytest.raises(ValidationError, match="GITHUB_TOKEN is required"):
        make_settings(data_source="github", github_token="   ")


def test_live_mode_with_secrets(make_settings):
    settings = make_settings(
        data_source="github", llm_mode="live",
        github_token=GH_TOKEN, anthropic_api_key=ANTHROPIC_KEY,
    )
    assert settings.github_token.get_secret_value() == GH_TOKEN
    assert settings.secret_values() == [GH_TOKEN, ANTHROPIC_KEY]


def test_secrets_not_exposed_in_repr_or_dump(make_settings):
    settings = make_settings(github_token=GH_TOKEN, anthropic_api_key=ANTHROPIC_KEY)
    for text in (repr(settings), str(settings), settings.model_dump_json()):
        assert GH_TOKEN not in text
        assert ANTHROPIC_KEY not in text


@pytest.mark.parametrize("repo", ["home-assistant", "a/b/c", "owner/na me", "../etc"])
def test_invalid_target_repo_rejected(make_settings, repo):
    with pytest.raises(ValidationError, match="TARGET_REPO"):
        make_settings(target_repo=repo)
