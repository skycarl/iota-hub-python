"""The CLI: streams, exit codes, ``--json`` shapes, and never a leaked key.

Every test drives the real typer app through ``CliRunner`` against the same
in-process ``MockAPI`` the library tests use — ``_make_client`` is the one seam,
so the command wiring under test is the wiring that ships.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx
import pytest
from conftest import API_KEY, BASE_URL, FIXTURES, MockAPI
from typer.testing import CliRunner

from iota_hub import DEFAULT_BASE_URL, Client, config
from iota_hub import cli as cli_module
from iota_hub.cli import app, exit_code_for
from iota_hub.errors import (
    AuthError,
    ConflictError,
    IotaHubError,
    RateLimitError,
    TransportError,
)

OBSERVATION_DIR = FIXTURES / "observation"
REPORT = OBSERVATION_DIR / "20180305_9721_Doty_Observer_POS.xlsx"
LIGHTCURVE = OBSERVATION_DIR / "20180305_9721_Doty_Observer_POS.csv"
LOG = OBSERVATION_DIR / "20180305_9721_Doty_Observer_POS_pyote_log.txt"

OBS_ID = "obs_1"
OBSERVATIONS = "/public/v1/observations"
DRAFTS = f"{OBSERVATIONS}/drafts"
GET = f"{OBSERVATIONS}/{OBS_ID}"
FINALIZE = f"{GET}/finalize"
SUBMIT = f"{GET}/submit"
RUNS = f"{GET}/validation/runs"
EVENTS = "/public/v1/events"
BUCKET = "/staging-bucket"

#: A second deployment, for the options that pick one.
OTHER_URL = "https://other.example.test"


# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the config file at a tmp path, so no test touches a real one."""
    path = tmp_path / "config.toml"
    monkeypatch.setattr(
        config, "config_file_path", lambda given=None: Path(given) if given else path
    )
    return path


@pytest.fixture(autouse=True)
def cli_env(
    monkeypatch: pytest.MonkeyPatch,
    mock_api: MockAPI,
    sleeps: list[float],
    config_file: Path,
) -> None:
    """A configured, colourless CLI whose client talks to the mock API."""
    monkeypatch.setenv("IOTA_HUB_API_KEY", API_KEY)
    monkeypatch.setenv("IOTA_HUB_BASE_URL", BASE_URL)
    monkeypatch.delenv("IOTA_HUB_PROFILE", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(
        cli_module,
        "_make_client",
        lambda settings: Client(
            settings.api_key,
            settings.base_url,
            transport=mock_api.transport,
            sleep=sleeps.append,
        ),
    )


# -- scripting the API ------------------------------------------------------


def observation(**overrides):
    body = {
        "observation_id": OBS_ID,
        "submission_status": "draft",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T03:04:05Z",
        "version": 4,
        "observer_user_id": "user_1",
        "metadata": {"observer_name": "Test Observer", "asteroid_name": "Doty"},
        "next_actions": [],
    }
    body.update(overrides)
    return body


def ready(**overrides):
    body = {"state": "ready", "checks_current": True}
    body.update(overrides)
    return body


def checks(**overrides):
    body = {"run_id": "run_1", "status": "complete", "checks_current": True}
    body.update(overrides)
    return body


def finding(**overrides):
    body = {
        "check_number": 7,
        "code": "lightcurve_time_gap",
        "name": "Light curve time gap",
        "severity": "error",
        "message": "The light curve has a 4 s gap at 05:12:00.",
        "how_to_fix": "Re-export the light curve from PyOTE without trimming.",
        "doc_url": "https://iota-hub.com/developers/checks#lightcurve_time_gap",
        "fingerprint": "9f2a1c",
    }
    body.update(overrides)
    return body


def upload_target(slot: str, filename: str):
    file_key = f"staging/{OBS_ID}/{slot}/{filename}"
    return {
        "slot": slot,
        "filename": filename,
        "upload_url": f"https://s3.example.test{BUCKET}",
        "fields": {"key": file_key, "policy": "b64policy"},
        "file_key": file_key,
    }


def script_submit(mock_api: MockAPI, final: dict) -> None:
    """Create, three uploads, finalize, one poll that is already terminal."""
    mock_api.json(
        "POST",
        DRAFTS,
        {
            "observation": observation(),
            "uploads": [
                upload_target("report", REPORT.name),
                upload_target("lightcurve", LIGHTCURVE.name),
                upload_target("log", LOG.name),
            ],
        },
        status=201,
    )
    mock_api.add("POST", BUCKET, httpx.Response(204))
    mock_api.json("POST", FINALIZE, observation())
    mock_api.json("GET", GET, final)


def run(runner: CliRunner, *args: str, **kwargs):
    return runner.invoke(app, list(args), **kwargs)


# -- exit codes -------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (IotaHubError("internal_error", "boom", status=500), 1),
        (TransportError("connect failed"), 1),
        (IotaHubError("upload_failed", "S3 said no"), 1),
        (IotaHubError("ambiguous_files", "two csvs"), 2),
        (IotaHubError("missing_files", "no report"), 2),
        (IotaHubError("invalid_extension", "not a csv"), 2),
        (IotaHubError("not_a_directory", "nope"), 2),
        (IotaHubError("unknown_profile", "no such profile"), 2),
        (IotaHubError("confirmation_required", "ask first"), 2),
        (AuthError("missing_api_key", "no key"), 3),
        (AuthError("expired_api_key", "expired", status=401), 3),
        (IotaHubError("timeout", "still running"), 4),
        (ConflictError("open_findings", "two findings", status=409), 4),
        (ConflictError("checks_stale", "stale", status=409), 4),
        (ConflictError("event_files_conflict", "collision", status=409), 4),
        (IotaHubError("missing_required", "no lightcurve", status=422), 4),
        (RateLimitError("rate_limited", "slow down", status=429), 5),
    ],
)
def test_exit_code_for(error: IotaHubError, expected: int) -> None:
    assert exit_code_for(error) == expected


def test_version(runner: CliRunner) -> None:
    result = run(runner, "--version")
    assert result.exit_code == 0
    assert result.stdout.startswith("iota-hub ")
    assert "(public API v1)" in result.stdout


# -- submit --dry-run -------------------------------------------------------


def test_dry_run_maps_the_folder_without_calling_anything(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    result = run(runner, "submit", str(OBSERVATION_DIR), "--dry-run")
    assert result.exit_code == 0
    assert mock_api.requests == []
    assert REPORT.name in result.stdout
    assert LIGHTCURVE.name in result.stdout
    assert LOG.name in result.stdout
    assert str(REPORT.stat().st_size) in result.stdout
    assert "Target:" in result.stdout


def test_dry_run_needs_no_key(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("IOTA_HUB_API_KEY")
    result = run(runner, "submit", str(OBSERVATION_DIR), "--dry-run")
    assert result.exit_code == 0


def test_dry_run_json_shape(runner: CliRunner, tmp_path: Path) -> None:
    folder = tmp_path / "obs"
    folder.mkdir()
    for source in (REPORT, LIGHTCURVE, LOG):
        (folder / source.name).write_bytes(source.read_bytes())
    (folder / "field_notes.png").write_bytes(b"png")

    result = run(runner, "--json", "submit", str(folder), "--dry-run")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["target"] == BASE_URL
    assert [upload["slot"] for upload in payload["uploads"]] == [
        "report",
        "lightcurve",
        "log",
    ]
    assert payload["ignored"] == ["field_notes.png"]


# -- submit -----------------------------------------------------------------


def test_submit_happy_path(runner: CliRunner, mock_api: MockAPI) -> None:
    script_submit(
        mock_api,
        observation(
            readiness=ready(),
            checks={"run_id": "r1", "status": "complete"},
            next_actions=["submit"],
        ),
    )
    mock_api.json(
        "POST", SUBMIT, observation(submission_status="submitted", next_actions=[])
    )

    result = run(runner, "submit", str(OBSERVATION_DIR))
    assert result.exit_code == 0
    assert OBS_ID in result.stdout
    assert "submitted" in result.stdout
    assert LIGHTCURVE.name in result.stdout
    # Progress belongs on stderr, and stdout stays data.
    assert "Creating draft" in result.stderr
    assert "Creating draft" not in result.stdout


def test_submit_json_is_one_document(runner: CliRunner, mock_api: MockAPI) -> None:
    script_submit(
        mock_api,
        observation(readiness=ready(), checks={"run_id": "r1", "status": "complete"}),
    )
    mock_api.json(
        "POST", SUBMIT, observation(submission_status="submitted", next_actions=[])
    )

    result = run(runner, "--json", "submit", str(OBSERVATION_DIR))
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "observation_id",
        "outcome",
        "submitted",
        "uploads",
        "ignored",
        "next_commands",
        "observation",
    }
    assert payload["observation_id"] == OBS_ID
    assert payload["outcome"] == "submitted"
    assert payload["submitted"] is True
    assert payload["uploads"]["lightcurve"] == LIGHTCURVE.name
    assert payload["observation"]["observation_id"] == OBS_ID


def test_submit_draft_stops_before_submitting(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    script_submit(
        mock_api,
        observation(
            readiness=ready(),
            checks={"run_id": "r1", "status": "complete"},
            next_actions=["submit"],
        ),
    )
    result = run(runner, "--json", "submit", str(OBSERVATION_DIR), "--draft")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["outcome"] == "ready"
    assert payload["submitted"] is False
    assert payload["next_commands"] == [f"iota-hub drafts submit {OBS_ID}"]
    assert not [r for r in mock_api.requests if r.url.path == SUBMIT]


def test_submit_needs_attention_exits_4(runner: CliRunner, mock_api: MockAPI) -> None:
    script_submit(
        mock_api,
        observation(
            readiness=ready(
                state="needs_attention", open_findings=1, missing_required=["log"]
            ),
            checks={
                "run_id": "r1",
                "status": "complete",
                "open_findings": 1,
                "findings": [finding()],
            },
            next_actions=["dismiss_or_fix"],
        ),
    )
    result = run(runner, "submit", str(OBSERVATION_DIR))
    assert result.exit_code == 4
    # check number, severity, code, message, how_to_fix, doc_url, fingerprint
    assert "[7] error lightcurve_time_gap" in result.stdout
    assert "4 s gap at 05:12:00" in result.stdout
    assert "how to fix: Re-export" in result.stdout
    assert "doc: https://iota-hub.com/developers/checks" in result.stdout
    assert "fingerprint: 9f2a1c" in result.stdout
    assert "Missing:" in result.stdout
    assert "log" in result.stdout
    assert "Next:" in result.stdout
    assert f"iota-hub drafts dismiss {OBS_ID} 9f2a1c" in result.stdout
    # The busiest human page there is, and still plain ASCII with no escapes.
    text = result.stdout + result.stderr
    text.encode("ascii")
    assert "\x1b" not in text


def test_submit_needs_attention_json_carries_next_commands(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    script_submit(
        mock_api,
        observation(
            readiness=ready(state="needs_attention", open_findings=1),
            checks={
                "run_id": "r1",
                "status": "complete",
                "open_findings": 1,
                "findings": [finding()],
            },
            next_actions=["dismiss_or_fix"],
        ),
    )
    result = run(runner, "--json", "submit", str(OBSERVATION_DIR))
    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["outcome"] == "needs_attention"
    assert any("drafts dismiss" in line for line in payload["next_commands"])


def test_submit_with_two_lightcurves_is_a_usage_error(
    runner: CliRunner, tmp_path: Path, mock_api: MockAPI
) -> None:
    folder = tmp_path / "obs"
    folder.mkdir()
    for source in (REPORT, LIGHTCURVE, LOG):
        (folder / source.name).write_bytes(source.read_bytes())
    (folder / "second.csv").write_text("t,flux\n")

    result = run(runner, "submit", str(folder))
    assert result.exit_code == 2
    assert mock_api.requests == []
    assert "error: ambiguous_files:" in result.stderr
    assert "--lightcurve" in result.stderr
    assert result.stdout == ""


def test_submit_no_wait_returns_a_draft(runner: CliRunner, mock_api: MockAPI) -> None:
    script_submit(mock_api, observation())
    result = run(runner, "--json", "submit", str(OBSERVATION_DIR), "--no-wait")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["outcome"] == "draft"


# -- errors -----------------------------------------------------------------


def test_missing_key_exits_3(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("IOTA_HUB_API_KEY")
    result = run(runner, "drafts", "list")
    assert result.exit_code == 3
    assert "error: missing_api_key:" in result.stderr
    assert "hint:" in result.stderr
    assert result.stdout == ""


def test_missing_key_json_error_on_stderr(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("IOTA_HUB_API_KEY")
    result = run(runner, "--json", "drafts", "list")
    assert result.exit_code == 3
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["code"] == "missing_api_key"
    assert payload["exit_code"] == 3
    assert payload["hint"]


def test_rate_limited_exits_5(
    runner: CliRunner, mock_api: MockAPI, sleeps: list[float]
) -> None:
    mock_api.json(
        "GET",
        OBSERVATIONS,
        {"code": "rate_limited", "message": "Too many requests."},
        status=429,
        headers={"Retry-After": "3"},
    )
    result = run(runner, "observations", "list")
    assert result.exit_code == 5
    # 1 + max_retries attempts, then the mapped error.
    assert len([r for r in mock_api.requests if r.url.path == OBSERVATIONS]) == 5
    assert sleeps == [3.0, 3.0, 3.0, 3.0]
    assert "error: rate_limited:" in result.stderr
    assert "retry after 3 s" in result.stderr


def test_unknown_profile_is_a_usage_error(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("IOTA_HUB_API_KEY")
    result = run(runner, "--profile", "nope", "drafts", "list")
    assert result.exit_code == 2
    assert "error: unknown_profile:" in result.stderr


# -- the target line --------------------------------------------------------


def test_target_is_printed_only_when_it_is_not_the_default(
    runner: CliRunner, mock_api: MockAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_api.json("GET", OBSERVATIONS, {"items": [], "next_cursor": None})
    result = run(runner, "observations", "list")
    assert f"Target: {BASE_URL}" in result.stderr
    assert "Target:" not in result.stdout

    monkeypatch.setenv("IOTA_HUB_BASE_URL", DEFAULT_BASE_URL)
    result = run(runner, "observations", "list")
    assert "Target:" not in result.stderr


# -- auth -------------------------------------------------------------------


def test_auth_login_reads_the_key_from_stdin_and_never_echoes_it(
    runner: CliRunner, mock_api: MockAPI, config_file: Path
) -> None:
    secret = "iotahub_k1_supersecrettail"
    mock_api.json("GET", OBSERVATIONS, {"items": [], "next_cursor": None})

    result = run(runner, "auth", "login", "--profile", "dev", input=f"{secret}\n")
    assert result.exit_code == 0
    assert secret not in result.stdout + result.stderr
    assert "iotahub_k1_..." in result.stdout
    stored = config_file.read_text(encoding="utf-8")
    assert secret in stored
    assert BASE_URL in stored


def test_auth_login_rejects_a_key_that_is_not_one(
    runner: CliRunner, config_file: Path
) -> None:
    result = run(runner, "auth", "login", input="hunter2\n")
    assert result.exit_code == 3
    assert "malformed_api_key" in result.stderr
    assert not config_file.exists()


def test_auth_status_reports_the_live_check(
    runner: CliRunner, mock_api: MockAPI, config_file: Path
) -> None:
    mock_api.json("GET", OBSERVATIONS, {"items": [], "next_cursor": None})
    result = run(runner, "--json", "auth", "status")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["check"] == "valid"
    assert payload["key_source"] == "env"
    assert payload["base_url"] == BASE_URL
    assert payload["key_prefix"] == "iotahub_test_..."
    assert payload["config_file"] == str(config_file)
    assert API_KEY not in result.stdout


def test_auth_status_names_the_auth_failure_without_failing(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    mock_api.json(
        "GET",
        OBSERVATIONS,
        {"code": "expired_api_key", "message": "That key expired."},
        status=401,
    )
    result = run(runner, "auth", "status")
    assert result.exit_code == 0
    assert "expired_api_key" in result.stdout


def test_auth_logout_forgets_the_profile(
    runner: CliRunner, mock_api: MockAPI, config_file: Path
) -> None:
    mock_api.json("GET", OBSERVATIONS, {"items": [], "next_cursor": None})
    run(runner, "auth", "login", "--profile", "dev", input="iotahub_k1_tail\n")
    result = run(runner, "--json", "auth", "logout", "--profile", "dev")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["deleted"] is True
    assert "iotahub_k1_tail" not in config_file.read_text(encoding="utf-8")


# -- drafts -----------------------------------------------------------------


def test_drafts_list_table(runner: CliRunner, mock_api: MockAPI) -> None:
    mock_api.json(
        "GET",
        OBSERVATIONS,
        {
            "items": [
                observation(
                    readiness=ready(
                        state="needs_attention",
                        open_findings=2,
                        missing_required=["log"],
                    )
                )
            ],
            "next_cursor": None,
        },
    )
    result = run(runner, "drafts", "list")
    assert result.exit_code == 0
    assert "OBSERVATION" in result.stdout
    assert OBS_ID in result.stdout
    assert "needs_attention" in result.stdout
    request = mock_api.last_request
    assert request.url.params["submission_status"] == "draft"


def test_drafts_show_renders_findings_and_next(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    mock_api.json(
        "GET",
        GET,
        observation(
            readiness=ready(state="needs_attention", open_findings=1),
            checks={
                "run_id": "r1",
                "status": "complete",
                "open_findings": 1,
                "findings": [finding()],
            },
            next_actions=["dismiss_or_fix"],
        ),
    )
    result = run(runner, "drafts", "show", OBS_ID)
    assert result.exit_code == 0
    assert "lightcurve_time_gap" in result.stdout
    assert "Next:" in result.stdout


def test_drafts_submit_refuses_a_draft_that_is_not_ready(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    mock_api.json(
        "GET",
        GET,
        observation(readiness=ready(state="needs_attention", open_findings=2)),
    )
    result = run(runner, "drafts", "submit", OBS_ID)
    assert result.exit_code == 4
    assert "error: open_findings:" in result.stderr
    assert not [r for r in mock_api.requests if r.url.path == SUBMIT]


def test_drafts_submit_sends_the_version_it_read(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    mock_api.json("GET", GET, observation(readiness=ready()))
    mock_api.json("POST", SUBMIT, observation(submission_status="submitted"))
    result = run(runner, "drafts", "submit", OBS_ID)
    assert result.exit_code == 0
    request = next(r for r in mock_api.requests if r.url.path == SUBMIT)
    assert json.loads(request.content) == {"version": 4}
    assert request.headers["Idempotency-Key"]


def test_drafts_delete_will_not_prompt_a_pipe(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    result = run(runner, "drafts", "delete", OBS_ID)
    assert result.exit_code == 2
    assert "--yes" in result.stderr
    assert mock_api.requests == []


def test_drafts_delete_with_yes(runner: CliRunner, mock_api: MockAPI) -> None:
    mock_api.add("DELETE", GET, httpx.Response(204))
    result = run(runner, "--json", "drafts", "delete", OBS_ID, "--yes")
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"observation_id": OBS_ID, "deleted": True}


def test_drafts_dismiss_sends_the_note(runner: CliRunner, mock_api: MockAPI) -> None:
    dismissed = finding(
        dismissal={
            "dismissed_by": "user_1",
            "dismissed_at": "2026-01-02T00:00:00Z",
            "note": "Known clock offset",
            "via": "api",
        }
    )
    mock_api.json(
        "POST",
        f"{GET}/validation/findings/9f2a1c/dismiss",
        {
            "run_id": "r1",
            "status": "complete",
            "open_findings": 0,
            "dismissed_findings": 1,
            "findings": [dismissed],
        },
    )
    result = run(
        runner, "drafts", "dismiss", OBS_ID, "9f2a1c", "--note", "Known clock offset"
    )
    assert result.exit_code == 0
    assert json.loads(mock_api.last_request.content) == {"note": "Known clock offset"}
    assert "(dismissed)" in result.stdout


def test_drafts_dismiss_without_a_note_is_a_usage_error(runner: CliRunner) -> None:
    result = run(runner, "drafts", "dismiss", OBS_ID, "9f2a1c")
    assert result.exit_code == 2
    assert "--note" in result.stderr


def test_drafts_dismiss_undo(runner: CliRunner, mock_api: MockAPI) -> None:
    mock_api.json(
        "DELETE",
        f"{GET}/validation/findings/9f2a1c/dismiss",
        {"run_id": "r1", "status": "complete", "open_findings": 1},
    )
    result = run(runner, "drafts", "dismiss", OBS_ID, "9f2a1c", "--undo")
    assert result.exit_code == 0
    assert mock_api.last_request.method == "DELETE"


def test_drafts_files_add_replaces_one_slot(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    mock_api.json(
        "POST",
        f"{GET}/files/lightcurve/upload/init",
        upload_target("lightcurve", LIGHTCURVE.name),
    )
    mock_api.add("POST", BUCKET, httpx.Response(204))
    mock_api.json(
        "POST",
        f"{GET}/files/lightcurve/upload/finalize",
        observation(readiness=ready()),
    )
    result = run(
        runner, "drafts", "files", "add", OBS_ID, "lightcurve", str(LIGHTCURVE)
    )
    assert result.exit_code == 0
    assert "ready" in result.stdout
    assert "Uploading lightcurve" in result.stderr


def test_drafts_files_rm_needs_yes_on_a_pipe(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    result = run(runner, "drafts", "files", "rm", OBS_ID, "vizier")
    assert result.exit_code == 2
    assert mock_api.requests == []


def test_drafts_files_rm_with_yes(runner: CliRunner, mock_api: MockAPI) -> None:
    mock_api.json("DELETE", f"{GET}/files/vizier", observation())
    result = run(runner, "drafts", "files", "rm", OBS_ID, "vizier", "--yes")
    assert result.exit_code == 0


def test_an_unknown_slot_is_rejected_before_any_call(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    result = run(runner, "drafts", "files", "rm", OBS_ID, "spectrum", "--yes")
    assert result.exit_code == 2
    assert mock_api.requests == []


# -- observations and events ------------------------------------------------


def test_observations_list_json_and_filters(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    mock_api.json(
        "GET",
        OBSERVATIONS,
        {"items": [observation(submission_status="submitted")], "next_cursor": "c2"},
    )
    result = run(
        runner,
        "--json",
        "observations",
        "list",
        "--observer-name",
        "Ada",
        "--observer-name",
        "Grace",
        "--result-type",
        "positive",
        "--ungrouped-only",
        "--limit",
        "5",
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["next_cursor"] == "c2"
    assert payload["items"][0]["observation_id"] == OBS_ID

    params = mock_api.last_request.url.params
    assert params.get_list("observer_names") == ["Ada", "Grace"]
    assert params["result_type"] == "positive"
    assert params["ungrouped_only"] == "true"
    assert params["limit"] == "5"


def test_observations_show(runner: CliRunner, mock_api: MockAPI) -> None:
    mock_api.json("GET", GET, observation(submission_status="submitted"))
    result = run(runner, "observations", "show", OBS_ID)
    assert result.exit_code == 0
    assert OBS_ID in result.stdout


def event(event_id: str, **overrides):
    body = {
        "event_id": event_id,
        "status": "COMPLETE",
        "asteroid_id": "9721",
        "asteroid_name": "Doty",
        "star_catalog": "UCAC4",
        "star_id": "123-456789",
        "date_window_utc": "2018-03-05",
        "observation_ids": [OBS_ID],
        "observer_names": ["Test Observer"],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }
    body.update(overrides)
    return body


def test_events_list_all_follows_the_cursor(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    mock_api.json("GET", EVENTS, {"items": [event("evt_1")], "next_cursor": "page2"})
    mock_api.json("GET", EVENTS, {"items": [event("evt_2")], "next_cursor": None})

    result = run(
        runner, "--json", "events", "list", "--status", "COMPLETE,DELIVERED", "--all"
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert [item["event_id"] for item in payload["items"]] == ["evt_1", "evt_2"]
    assert payload["next_cursor"] is None

    requests = [r for r in mock_api.requests if r.url.path == EVENTS]
    assert len(requests) == 2
    assert requests[0].url.params["status"] == "COMPLETE,DELIVERED"
    assert requests[1].url.params["cursor"] == "page2"


def test_events_show(runner: CliRunner, mock_api: MockAPI) -> None:
    mock_api.json("GET", f"{EVENTS}/evt_1", event("evt_1"))
    result = run(runner, "events", "show", "evt_1")
    assert result.exit_code == 0
    assert "evt_1" in result.stdout
    assert "UCAC4" in result.stdout


# -- files download ---------------------------------------------------------


def test_files_download_writes_every_link(
    runner: CliRunner, mock_api: MockAPI, tmp_path: Path
) -> None:
    mock_api.json(
        "GET",
        f"{GET}/files",
        {
            "observation_id": OBS_ID,
            "expires_in": 900,
            "files": [
                {
                    "slot": "lightcurve",
                    "filename": LIGHTCURVE.name,
                    "size_bytes": 10,
                    "url": "https://s3.example.test/get/lc.csv",
                    "expires_in": 900,
                }
            ],
        },
    )
    mock_api.add("GET", "/get/lc.csv", httpx.Response(200, content=b"t,flux\n"))

    out = tmp_path / "downloads"
    result = run(
        runner, "--json", "files", "download", "observation", OBS_ID, "-o", str(out)
    )
    assert result.exit_code == 0
    written = json.loads(result.stdout)["files"]
    assert len(written) == 1
    assert (out / LIGHTCURVE.name).read_bytes() == b"t,flux\n"
    # The presigned GET must not carry the key.
    fetch = next(r for r in mock_api.requests if r.url.path == "/get/lc.csv")
    assert "X-API-Key" not in fetch.headers


def test_files_download_refuses_to_overwrite(
    runner: CliRunner, mock_api: MockAPI, tmp_path: Path
) -> None:
    mock_api.json(
        "GET",
        f"{GET}/files",
        {
            "observation_id": OBS_ID,
            "expires_in": 900,
            "files": [
                {
                    "slot": "lightcurve",
                    "filename": "lc.csv",
                    "size_bytes": 10,
                    "url": "https://s3.example.test/get/lc.csv",
                    "expires_in": 900,
                }
            ],
        },
    )
    out = tmp_path / "downloads"
    out.mkdir()
    (out / "lc.csv").write_text("already here")

    result = run(runner, "files", "download", "observation", OBS_ID, "-o", str(out))
    assert result.exit_code == 1
    assert "error: file_exists:" in result.stderr
    assert "--force" in result.stderr


# -- guide ------------------------------------------------------------------


def test_guide_prints_something(runner: CliRunner) -> None:
    result = run(runner, "guide")
    assert result.exit_code == 0
    assert result.stdout.strip()


# -- output hygiene ---------------------------------------------------------

HUMAN_RUNS = [
    ["submit", str(OBSERVATION_DIR), "--dry-run"],
    ["--version"],
    ["--help"],
    ["submit", "--help"],
    ["drafts", "--help"],
    ["drafts", "files", "add", "--help"],
    ["observations", "list", "--help"],
    ["events", "list", "--help"],
    ["files", "download", "--help"],
    ["auth", "login", "--help"],
]


@pytest.mark.parametrize("args", HUMAN_RUNS, ids=lambda a: " ".join(a))
def test_human_output_is_plain_ascii_without_escape_codes(
    runner: CliRunner, args: list[str]
) -> None:
    result = run(runner, *args)
    text = result.stdout + result.stderr
    text.encode("ascii")  # raises if anything decorative crept in
    assert "\x1b" not in text


def test_every_command_help_carries_an_example(runner: CliRunner) -> None:
    commands = [
        ["submit"],
        ["guide"],
        ["auth", "login"],
        ["auth", "status"],
        ["auth", "logout"],
        ["drafts", "list"],
        ["drafts", "show"],
        ["drafts", "check"],
        ["drafts", "submit"],
        ["drafts", "delete"],
        ["drafts", "dismiss"],
        ["drafts", "files", "add"],
        ["drafts", "files", "rm"],
        ["observations", "list"],
        ["observations", "show"],
        ["events", "list"],
        ["events", "show"],
        ["files", "download"],
    ]
    for command in commands:
        result = run(runner, *command, "--help")
        assert result.exit_code == 0, command
        assert "Example: iota-hub" in result.stdout, command


# -- the shared options, before or after the subcommand ---------------------


def test_json_is_the_same_run_before_or_after_the_subcommand(
    runner: CliRunner,
) -> None:
    leading = run(runner, "--json", "submit", "--dry-run", str(OBSERVATION_DIR))
    trailing = run(runner, "submit", "--dry-run", str(OBSERVATION_DIR), "--json")
    assert leading.exit_code == trailing.exit_code == 0
    assert json.loads(leading.stdout) == json.loads(trailing.stdout)


@pytest.mark.parametrize("args", [["--base-url", OTHER_URL], ["--profile", "other"]])
def test_the_target_options_work_after_the_subcommand(
    runner: CliRunner,
    mock_api: MockAPI,
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    # The environment would beat the profile, so the profile has to stand alone.
    monkeypatch.delenv("IOTA_HUB_BASE_URL")
    config_file.write_text(
        f'[profiles.other]\nbase_url = "{OTHER_URL}"\napi_key = "{API_KEY}"\n',
        encoding="utf-8",
    )
    mock_api.json("GET", OBSERVATIONS, {"items": [], "next_cursor": None})
    result = run(runner, "observations", "list", *args)
    assert result.exit_code == 0
    assert f"Target: {OTHER_URL}" in result.stderr


def test_a_leaf_option_beats_the_root_one(runner: CliRunner, mock_api: MockAPI) -> None:
    mock_api.json("GET", OBSERVATIONS, {"items": [], "next_cursor": None})
    result = run(
        runner, "--base-url", BASE_URL, "observations", "list", "--base-url", OTHER_URL
    )
    assert result.exit_code == 0
    assert f"Target: {OTHER_URL}" in result.stderr


# -- prompts are not data ---------------------------------------------------


class _Tty(io.StringIO):
    """A stdin that claims to be a terminal, so the prompts are reached."""

    def isatty(self) -> bool:
        return True


def test_the_key_prompt_and_the_confirmation_are_written_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: dict[str, dict] = {}

    def fake_prompt(text: str, **kwargs) -> str:
        asked["prompt"] = kwargs
        return "iotahub_key_secret"

    def fake_confirm(text: str, **kwargs) -> bool:
        asked["confirm"] = kwargs
        return True

    monkeypatch.setattr(cli_module.sys, "stdin", _Tty())
    monkeypatch.setattr(cli_module.typer, "prompt", fake_prompt)
    monkeypatch.setattr(cli_module.typer, "confirm", fake_confirm)

    assert cli_module._read_key() == "iotahub_key_secret"
    cli_module._confirm("Delete draft obs_1?", False)
    assert asked["prompt"]["err"] is True
    assert asked["confirm"]["err"] is True


# -- drafts submit and drafts check -----------------------------------------


def test_drafts_submit_refuses_an_observation_that_is_already_submitted(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    mock_api.json(
        "GET",
        GET,
        observation(submission_status="submitted", readiness=None),
    )
    result = run(runner, "drafts", "submit", OBS_ID)
    assert result.exit_code == 1
    assert "error: already_submitted:" in result.stderr
    assert not [r for r in mock_api.requests if r.url.path == SUBMIT]


def test_drafts_check_waits_for_the_run_that_is_already_under_way(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    mock_api.json(
        "POST",
        RUNS,
        {
            "code": "check_run_in_progress",
            "message": "A validation run is already in progress.",
        },
        status=409,
    )
    mock_api.json("GET", GET, observation(checks=checks(status="complete")))
    result = run(runner, "drafts", "check", OBS_ID)
    assert result.exit_code == 0
    assert [r for r in mock_api.requests if r.url.path == GET]


def test_drafts_check_no_wait_says_a_run_is_already_under_way(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    mock_api.json(
        "POST",
        RUNS,
        {
            "code": "check_run_in_progress",
            "message": "A validation run is already in progress.",
        },
        status=409,
    )
    mock_api.json("GET", GET, observation(checks=checks(status="running")))
    result = run(runner, "drafts", "check", OBS_ID, "--no-wait")
    assert result.exit_code == 0
    assert "already under way" in result.stderr


def test_drafts_check_still_fails_on_another_conflict(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    mock_api.json(
        "POST",
        RUNS,
        {"code": "missing_required", "message": "No lightcurve."},
        status=409,
    )
    result = run(runner, "drafts", "check", OBS_ID)
    assert result.exit_code == 4
    assert "error: missing_required:" in result.stderr


# -- downloads name server-side slots too -----------------------------------


def test_files_download_takes_a_slot_the_server_names(
    runner: CliRunner, mock_api: MockAPI, tmp_path: Path
) -> None:
    mock_api.json(
        "GET",
        f"{GET}/files",
        {
            "observation_id": OBS_ID,
            "expires_in": 900,
            "files": [
                {
                    "slot": "damit",
                    "filename": "model.png",
                    "size_bytes": 3,
                    "url": "https://s3.example.test/get/model.png",
                    "expires_in": 900,
                },
                {
                    "slot": "lightcurve",
                    "filename": "lc.csv",
                    "size_bytes": 3,
                    "url": "https://s3.example.test/get/lc.csv",
                    "expires_in": 900,
                },
            ],
        },
    )
    mock_api.add("GET", "/get/model.png", httpx.Response(200, content=b"png"))

    out = tmp_path / "downloads"
    result = run(
        runner,
        "files",
        "download",
        "observation",
        OBS_ID,
        "--slot",
        "damit",
        "-o",
        str(out),
        "--json",
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["files"] == [str(out / "model.png")]


# -- the JSON error object --------------------------------------------------


def test_the_json_error_carries_status_and_retry_after(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    mock_api.json(
        "GET",
        OBSERVATIONS,
        {"code": "rate_limited", "message": "Too many requests."},
        status=429,
        headers={"Retry-After": "3"},
    )
    result = run(runner, "observations", "list", "--json")
    assert result.exit_code == 5
    # The `Target:` line shares stderr with the error document.
    payload = json.loads(result.stderr[result.stderr.index("{") :])
    assert payload["status"] == 429
    assert payload["retry_after"] == 3.0
    assert payload["exit_code"] == 5


def test_a_usage_error_is_json_too_when_json_was_asked_for(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["iota-hub", "drafts", "show", "--json"])
    with pytest.raises(SystemExit) as exit_info:
        cli_module.main()
    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload == {
        "code": "usage_error",
        "message": "Missing argument 'ID'.",
        "hint": "Run the command with --help.",
        "details": {},
        "status": None,
        "retry_after": None,
        "exit_code": 2,
    }


def test_a_usage_error_without_json_is_clicks_own_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["iota-hub", "drafts", "show"])
    with pytest.raises(SystemExit) as exit_info:
        cli_module.main()
    assert exit_info.value.code == 2
    assert "Missing argument 'ID'." in capsys.readouterr().err
