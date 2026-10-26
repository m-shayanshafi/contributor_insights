"""Record Gemini narratives for every fixture contributor and period.

Usage: uv run python -m scripts.record_narratives [--pause 4]

Runs the real narrative endpoint in fixture mode, so the recorded input hashes are
exactly what fixture mode looks up. Already-recorded inputs are skipped, so the
script can be re-run after a free-tier quota error.
"""

import argparse
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from app.analytics.periods import Period
from app.config import Settings
from app.github.fixture_client import FixtureSource
from app.llm.gemini import GeminiLLM
from app.llm.recorded import RecordingLLM
from app.main import create_app
from app.narrative.service import NarrativeService
from app.services import open_services


def record(pause: float) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(data_source="fixtures", llm_mode="live",
                            db_path=Path(tmp) / "record.db", rate_limit="1000/minute")
        directory = settings.fixtures_dir / "narratives"

        @asynccontextmanager
        async def recording_services(settings: Settings):
            async with open_services(settings) as services:
                llm = GeminiLLM(settings.gemini_api_key, settings.gemini_model)
                services.narratives = NarrativeService(
                    services.repo, RecordingLLM(llm, directory), settings.target_repo)
                try:
                    yield services
                finally:
                    await llm.aclose()

        logins = [c.login for c in FixtureSource(settings.fixtures_dir).contributors()]
        failures = 0
        with TestClient(create_app(settings, services_factory=recording_services)) as client:
            for login in logins:
                for period in Period:
                    before = len(list(directory.glob("*.json"))) if directory.exists() else 0
                    response = client.get(f"/api/contributors/{login}/narrative?period={period}")
                    if response.status_code != 200:
                        failures += 1
                        print(f"{login} {period}: HTTP {response.status_code} (see log)")
                        continue
                    print(f"{login} {period}: ok")
                    if len(list(directory.glob("*.json"))) > before:
                        time.sleep(pause)  # stay under the free-tier requests-per-minute quota
        print(f"done, {failures} failed" + ("; re-run to retry them" if failures else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pause", type=float, default=4.0, help="seconds between Gemini calls")
    record(parser.parse_args().pause)


if __name__ == "__main__":
    main()
