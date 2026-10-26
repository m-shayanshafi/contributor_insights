import json

import pytest

from app.config import Settings
from app.github.fixture_client import MANIFEST, ContributorFixture, write_contributor
from app.store.records import PullRequest, Review

_ENV_VARS = (
    "DATA_SOURCE", "LLM_MODE", "GITHUB_TOKEN", "GEMINI_API_KEY", "TARGET_REPO", "DB_PATH",
    "FIXTURES_DIR",
)

RECORDED_AT = 1_800_000_000  # 2027-01-15T08:00:00Z
DAY = 86_400


def write_fixtures(directory, contributors, repo="home-assistant/core", logins=None):
    directory.mkdir(parents=True, exist_ok=True)
    for fixture in contributors:
        write_contributor(directory / f"{fixture.login}.json", fixture)
    manifest = {"repo": repo, "recorded_at": RECORDED_AT, "history_days": 180,
                "logins": logins if logins is not None else [c.login for c in contributors]}
    (directory / MANIFEST).write_text(json.dumps(manifest))
    return directory


def sample_fixture(login="Alice"):
    prs = [
        PullRequest(number=n, author_login=login, title=f"Fix {n}", created_at=merged - 3600,
                    updated_at=merged, state="MERGED", additions=50, deletions=10, merged_at=merged)
        for n, merged in [(1, RECORDED_AT - 2 * DAY), (2, RECORDED_AT - 40 * DAY)]
    ]
    reviews = [Review(review_id="R1", pr_number=99, pr_author_login="bob", reviewer_login=login,
                      state="APPROVED", submitted_at=RECORDED_AT - DAY)]
    return ContributorFixture(login, prs, reviews)


@pytest.fixture
def make_settings(monkeypatch, tmp_path):
    """Build Settings isolated from the developer's real environment and `.env`."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    fixtures_dir = write_fixtures(tmp_path / "fixtures", [sample_fixture()])

    def _make(**overrides) -> Settings:
        values = {
            "data_source": "fixtures",
            "llm_mode": "recorded",
            "db_path": tmp_path / "test.db",
            "fixtures_dir": fixtures_dir,
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return _make
