"""The live end-to-end test against a deployed IOTA Hub.

Skipped unless ``IOTA_HUB_API_KEY`` and ``IOTA_HUB_BASE_URL`` are both set, so
``pytest`` on a laptop or a fork PR never reaches for the network. CI runs it
as its own job with a dedicated dev observer account's key.

It **never submits**: a submitted observation cannot be deleted, and the smoke
account would accumulate them one run at a time. Everything it creates is a
draft, and every draft it creates is deleted in a ``finally``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from iota_hub import Client, IotaHubError, config

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "observation"
LIGHTCURVE = FIXTURE / "20180305_9721_Doty_Observer_POS.csv"

#: The fixture's observer. Only drafts carrying this name are ever cleaned up.
FIXTURE_OBSERVER = "Test Observer"

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        not (
            os.environ.get("IOTA_HUB_API_KEY") and os.environ.get("IOTA_HUB_BASE_URL")
        ),
        reason="needs IOTA_HUB_API_KEY and IOTA_HUB_BASE_URL",
    ),
]


@pytest.fixture
def client():
    """A client for the deployed target, with the fixture's leftovers cleared.

    The open-draft cap is 100, so a crashed earlier run must not leak: every
    draft this account owns under the fixture's observer name is deleted before
    the test starts. Only drafts: the listing is filtered to them, and the API
    refuses to delete anything submitted anyway.
    """
    settings = config.resolve()
    with Client(settings.api_key, settings.base_url) as client:
        _purge_fixture_drafts(client)
        yield client


def _purge_fixture_drafts(client: Client) -> None:
    for draft in list(client.iter_observations(submission_status="draft")):
        if draft.metadata.observer_name != FIXTURE_OBSERVER:
            continue
        try:
            client.delete_draft(draft.observation_id)
            print(f"purged leftover draft {draft.observation_id}")
        except IotaHubError as error:
            print(f"could not purge {draft.observation_id}: {error}")


def _delete(client: Client, observation_id: str) -> None:
    try:
        client.delete_draft(observation_id)
    except IotaHubError as error:
        print(f"could not delete {observation_id}: {error}")


def test_submit_folder_upload_list_and_download(client: Client, tmp_path: Path) -> None:
    """The whole workflow against a live deployment, minus the submit."""
    result = client.submit_folder(FIXTURE, submit=False, wait=True, timeout=600)
    observation_id = result.observation_id
    try:
        observation = result.observation
        state = observation.readiness.state if observation.readiness else None
        print(
            f"observation {observation_id} outcome={result.outcome} readiness={state}"
        )

        assert observation_id
        assert set(result.uploads) == {"report", "lightcurve", "log"}
        assert observation.checks is not None, "no check run on the finished draft"
        assert observation.checks.status in {"complete", "error"}
        assert observation.readiness is not None

        # The fix loop: init, upload, finalize, in one call.
        replaced = client.replace_file(observation_id, "lightcurve", LIGHTCURVE)
        assert replaced.observation_id == observation_id

        drafts = [
            draft.observation_id
            for draft in client.iter_observations(submission_status="draft")
        ]
        assert observation_id in drafts

        written = client.download_files("observation", observation_id, tmp_path)
        assert len(written) == 3
        assert all(path.exists() and path.stat().st_size > 0 for path in written)
    finally:
        _delete(client, observation_id)


def test_cli_submit_draft_json(client: Client) -> None:
    """The installed command, end to end, exactly as an agent would run it."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "iota_hub.cli",
            "submit",
            str(FIXTURE),
            "--draft",
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        check=False,
    )
    print(completed.stderr)
    assert completed.returncode in {0, 4}, completed.stderr

    payload = json.loads(completed.stdout)
    observation_id = payload["observation_id"]
    try:
        print(f"cli observation {observation_id} outcome={payload['outcome']}")
        assert payload["outcome"] in {"ready", "needs_attention"}
        assert payload["submitted"] is False
        assert set(payload["uploads"]) == {"report", "lightcurve", "log"}
        assert payload["observation"]["submission_status"] == "draft"
    finally:
        _delete(client, observation_id)
