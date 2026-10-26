"""End to end in fixture mode: the committed fixtures and recorded narratives, no credentials."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.narrative.grounding import resolve

FIXTURE_LOGINS = ["frenck", "joostlek", "rrooggiieerr"]  # heavy, review-focused, occasional


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    settings = Settings(_env_file=None, data_source="fixtures", llm_mode="recorded",
                        fixtures_dir=Path("fixtures"), rate_limit="1000/minute",
                        db_path=tmp_path_factory.mktemp("e2e") / "e2e.db")
    with TestClient(create_app(settings)) as client:
        yield client


def test_healthz(client):
    assert client.get("/healthz").json()["data_source"] == "fixtures"


@pytest.mark.parametrize("login", FIXTURE_LOGINS)
@pytest.mark.parametrize("period", ["1w", "1m", "3m"])
def test_both_endpoints_agree(client, login, period):
    metrics = client.get(f"/api/contributors/{login}?period={period}")
    narrative = client.get(f"/api/contributors/{login}/narrative?period={period}")
    assert metrics.status_code == 200 and narrative.status_code == 200
    body = narrative.json()
    assert body["metrics"] == metrics.json()["metrics"]
    assert body["window"] == metrics.json()["window"]
    assert body["narrative"] and body["self_comparison"] in {"Faster", "Slower", "Steady"}
    assert 0 <= body["confidence"] <= min(body["llm_confidence"], body["data_sufficiency"])


def test_evidence_refs_resolve_against_the_metrics(client):
    body = client.get("/api/contributors/joostlek/narrative?period=3m").json()
    assert body["evidence_chain"]
    data = {"metrics": body["metrics"]}
    for item in body["evidence_chain"]:
        if item["ref"].startswith("metrics."):
            value = resolve(data, item["ref"])
            assert float(item["value"].rstrip("%").lstrip("+")) == pytest.approx(value, abs=0.051)


def test_unknown_login_and_bad_input(client):
    assert client.get("/api/contributors/not-a-fixture-user/narrative").status_code == 404
    assert client.get("/api/contributors/frenck?period=12m").status_code == 422
