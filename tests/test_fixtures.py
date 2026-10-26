import pytest
from fastapi.testclient import TestClient

from app.github.fixture_client import FixtureSource, load_contributor
from app.github.validation import InvalidLogin
from app.main import create_app
from scripts.record_fixtures import scrub
from tests.conftest import RECORDED_AT, sample_fixture, write_fixtures


def test_round_trip(tmp_path):
    fixture = sample_fixture()
    write_fixtures(tmp_path, [fixture])
    assert load_contributor(tmp_path / "Alice.json") == fixture


async def test_source_matches_live_interface(tmp_path):
    source = FixtureSource(write_fixtures(tmp_path, [sample_fixture()]))
    assert await source.get_user("alice") == "Alice"
    prs = await source.authored_prs("alice", RECORDED_AT - 7 * 86_400, RECORDED_AT)
    assert [pr.number for pr in prs] == [1]
    (reviewed,) = await source.reviewed_prs("ALICE", 0, RECORDED_AT)
    assert reviewed.pr_number == 99 and reviewed.reviews[0].review_id == "R1"


def test_manifest_login_cannot_escape_directory(tmp_path):
    write_fixtures(tmp_path, [sample_fixture()], logins=["../../etc/passwd"])
    with pytest.raises(InvalidLogin):
        FixtureSource(tmp_path)


def test_fixture_mode_serves_without_any_token(make_settings):
    settings = make_settings()
    assert settings.github_token is None and settings.gemini_api_key is None
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/contributors/alice?period=1m")
        unknown = client.get("/api/contributors/someone-else")
    assert response.status_code == 200
    body = response.json()
    assert body["login"] == "Alice"
    assert body["window"]["to"] == "2027-01-15"  # clock frozen at recording time
    assert body["metrics"]["prs_merged"] == {"value": 1, "previous_period": 1, "pct_change": 0.0}
    assert body["metrics"]["reviews_given"]["value"] == 1
    assert body["meta"]["last_synced_at"] == "2027-01-15T08:00:00Z"
    assert unknown.status_code == 404


def test_seeded_contributors_do_not_count_toward_new_contributor_cap(make_settings, tmp_path):
    many = write_fixtures(tmp_path / "many", [sample_fixture(f"user{i}") for i in range(3)])
    settings = make_settings(fixtures_dir=many, max_new_contributors_per_hour=2)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/contributors/someone-else").status_code == 404


def test_fixtures_for_another_repo_fail_at_startup(make_settings, tmp_path):
    other = write_fixtures(tmp_path / "other", [sample_fixture()], repo="someone/else")
    with pytest.raises(RuntimeError, match="someone/else"):
        with TestClient(create_app(make_settings(fixtures_dir=other))):
            pass


def test_scrub_removes_emails_from_titles():
    fixture = sample_fixture()
    pr = fixture.pull_requests[0]
    dirty = type(fixture)(fixture.login, [type(pr)(**{**pr.__dict__, "title": "Thanks jane.doe@example.com"})], [])
    assert scrub(dirty).pull_requests[0].title == "Thanks [email]"
