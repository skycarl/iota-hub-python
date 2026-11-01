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
from rich.console import Console
from typer.testing import CliRunner

from iota_hub import DEFAULT_BASE_URL, Client, _term, config
from iota_hub import cli as cli_module
from iota_hub.cli import app, exit_code_for
from iota_hub.errors import (
    AuthError,
    ConflictError,
    IotaHubError,
    RateLimitError,
    RequestError,
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


def script_submit(
    mock_api: MockAPI, final: dict, *, finalized: dict | None = None
) -> None:
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
    mock_api.json("POST", FINALIZE, finalized or observation())
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
        # The amend refusals: a report for another observation is the wrong
        # file for this one (usage, like invalid_extension); the owner's lock
        # is a plain conflict -- waiting will not lift it.
        (RequestError("identity_changed", "other star", status=422), 2),
        (ConflictError("already_processed", "in review", status=409), 1),
        (ConflictError("already_submitted", "cannot delete", status=409), 1),
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
    assert "dry run, nothing sent" in result.stdout
    assert f"{REPORT.name}  (62.8 KB)" in result.stdout
    # Announced like every other command's target, on stderr...
    assert f"Target: {BASE_URL}" in result.stderr
    assert "Target:" not in result.stdout


def test_dry_run_announces_even_the_production_target(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ...and, unlike them, even when it is production: that is what it is for.
    monkeypatch.setenv("IOTA_HUB_BASE_URL", DEFAULT_BASE_URL)
    result = run(runner, "submit", str(OBSERVATION_DIR), "--dry-run")
    assert result.exit_code == 0
    assert f"Target: {DEFAULT_BASE_URL}" in result.stderr


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
    assert payload["attachments"] == [
        {
            "filename": "field_notes.png",
            "path": str(folder / "field_notes.png"),
            "size": 3,
        }
    ]
    assert payload["ignored"] == []


def test_dry_run_lists_attachments_and_no_attachments_ignores_them(
    runner: CliRunner, tmp_path: Path
) -> None:
    folder = tmp_path / "obs"
    folder.mkdir()
    for source in (REPORT, LIGHTCURVE, LOG):
        (folder / source.name).write_bytes(source.read_bytes())
    (folder / "X_Tangra.xml").write_bytes(b"lc")

    result = run(runner, "submit", str(folder), "--dry-run")
    assert result.exit_code == 0
    assert "attachment  X_Tangra.xml" in result.stdout

    result = run(runner, "submit", str(folder), "--dry-run", "--no-attachments")
    assert result.exit_code == 0
    assert "X_Tangra.xml" not in result.stdout.split("Not uploaded")[0]
    assert "Not uploaded (--no-attachments): X_Tangra.xml" in result.stdout


def test_attach_adds_a_file_to_the_dry_run(runner: CliRunner, tmp_path: Path) -> None:
    extra = tmp_path / "plot.png"
    extra.write_bytes(b"png")
    result = run(
        runner,
        "--json",
        "submit",
        str(OBSERVATION_DIR),
        "--dry-run",
        "--attach",
        str(extra),
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert [item["filename"] for item in payload["attachments"]] == ["plot.png"]


def test_a_blocked_attachment_fails_fast_with_exit_2(
    runner: CliRunner, mock_api: MockAPI, tmp_path: Path
) -> None:
    folder = tmp_path / "obs"
    folder.mkdir()
    for source in (REPORT, LIGHTCURVE, LOG):
        (folder / source.name).write_bytes(source.read_bytes())
    (folder / "reduce.py").write_text("print()\n")

    result = run(runner, "--json", "submit", str(folder))
    assert result.exit_code == 2
    assert mock_api.requests == []
    error = json.loads(result.stderr[result.stderr.index("{") :])
    assert error["code"] == "blocked_attachment"
    assert error["details"]["files"] == ["reduce.py"]

    # The dry run refuses the same folder, and --no-attachments settles it.
    assert run(runner, "submit", str(folder), "--dry-run").exit_code == 2
    assert (
        run(runner, "submit", str(folder), "--dry-run", "--no-attachments").exit_code
        == 0
    )


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
    # What was observed, as drafts submit / observations show print it.
    assert "Observer:     Test Observer" in result.stdout
    assert "Asteroid:     Doty" in result.stdout
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
        "attachments",
        "ignored",
        "next_commands",
        "skipped_checks",
        "owc_commands",
        "web_url",
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


def test_drafts_files_add_attachment(
    runner: CliRunner, mock_api: MockAPI, tmp_path: Path
) -> None:
    plot = tmp_path / "plot.png"
    plot.write_bytes(b"png")
    target = upload_target("attachment", "plot.png")
    target["attachment_id"] = "att-1"
    mock_api.json("POST", f"{GET}/attachments/upload/init", target)
    mock_api.add("POST", BUCKET, httpx.Response(204))
    mock_api.json(
        "POST",
        f"{GET}/attachments/upload/finalize",
        observation(
            attachments=[
                {
                    "attachment_id": "att-1",
                    "slot": "attachment",
                    "filename": "plot.png",
                    "size_bytes": 3,
                    "uploaded_at": "2026-01-01T00:00:00Z",
                }
            ]
        ),
    )
    result = run(runner, "drafts", "files", "add", OBS_ID, "attachment", str(plot))
    assert result.exit_code == 0, result.output
    assert "Attachments:" in result.stdout
    assert "att-1  plot.png  (3 B)" in result.stdout


def test_drafts_files_rm_attachment_needs_its_id(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    result = run(runner, "drafts", "files", "rm", OBS_ID, "attachment", "--yes")
    assert result.exit_code == 2
    assert mock_api.requests == []
    result = run(runner, "drafts", "files", "rm", OBS_ID, "vizier", "x", "--yes")
    assert result.exit_code == 2
    assert mock_api.requests == []


def test_drafts_files_rm_attachment(runner: CliRunner, mock_api: MockAPI) -> None:
    mock_api.json("DELETE", f"{GET}/attachments/att-1", observation())
    result = run(
        runner,
        "--json",
        "drafts",
        "files",
        "rm",
        OBS_ID,
        "attachment",
        "att-1",
        "--yes",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["attachments"] == []


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
    assert "Asteroid:     9721 Doty" in result.stdout
    assert "Star:         UCAC4 123-456789" in result.stdout


def test_observation_and_event_views_name_the_asteroid_and_star_alike(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    metadata = {
        "observer_name": "Test Observer",
        "asteroid_id": "9721",
        "asteroid_name": "Doty",
        "star_catalog": "UCAC4",
        "star_id": "123-456789",
    }
    mock_api.json("GET", GET, observation(metadata=metadata))
    result = run(runner, "observations", "show", OBS_ID)
    assert result.exit_code == 0
    assert "Asteroid:     9721 Doty" in result.stdout
    assert "Star:         UCAC4 123-456789" in result.stdout


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
    ["observations", "check", "--help"],
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
        ["observations", "check"],
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


def test_drafts_submit_of_an_already_submitted_observation_is_success(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    """Submit is a state you ask for: asking twice is success (API spec 4.2)."""
    mock_api.json(
        "GET",
        GET,
        observation(submission_status="submitted", status="submitted", readiness=None),
    )
    result = run(runner, "--json", "drafts", "submit", OBS_ID)
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "submitted"
    assert result.stderr.count("error") == 0
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


# -- skipped checks and the OWC link (warn, never block) ----------------------

OWC_A = "2018-9721-12345-678901-T0001"
OWC_B = "2018-9721-12345-678901-T0002"
SKIPPED_11 = {
    "check_number": 11,
    "code": "observation_predicted_time",
    "name": "Near predicted time",
    "status": "skipped",
    "reason": "No OWC match: 2 candidates - pick one.",
}


def ambiguous_owc(choice=None):
    return {
        "choice": choice,
        "owc_event_id": None,
        "owc_event_url": None,
        "resolution": {
            "status": "ambiguous",
            "reason": "2 OWC events match this asteroid and date.",
            "candidates": [
                {
                    "owc_event_id": OWC_A,
                    "owc_event_url": f"https://cloud.occultwatcher.net/event/{OWC_A}",
                    "asteroid": "(9721) Doty",
                    "star": "TYC 1",
                    "star_mag": 11.2,
                },
                {
                    "owc_event_id": OWC_B,
                    "owc_event_url": f"https://cloud.occultwatcher.net/event/{OWC_B}",
                },
            ],
        },
    }


def test_submit_warns_about_skipped_checks_and_the_owc_link_but_does_not_block(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    """A ready draft with check 11 skipped is still submitted -- loudly."""
    script_submit(
        mock_api,
        observation(
            readiness=ready(),
            checks=checks(skipped=[SKIPPED_11]),
            owc=ambiguous_owc(),
        ),
    )
    mock_api.json("POST", SUBMIT, observation(submission_status="submitted"))

    result = run(runner, "submit", str(OBSERVATION_DIR))

    assert result.exit_code == 0, result.output
    assert "WARNING: 1 check did not run" in result.stderr
    assert "[11] Near predicted time: No OWC match: 2 candidates" in result.stderr
    assert "WARNING: no OWC link (ambiguous)" in result.stderr
    assert f"iota-hub drafts owc {OBS_ID} --pick {OWC_A}" in result.stderr
    assert f"iota-hub drafts owc {OBS_ID} --none" in result.stderr
    assert "WARNING" not in result.stdout


def test_submit_json_carries_the_skipped_checks_and_owc_commands(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    script_submit(
        mock_api,
        observation(
            readiness=ready(),
            checks=checks(skipped=[SKIPPED_11]),
            owc=ambiguous_owc(),
        ),
    )

    result = run(runner, "--json", "submit", "--draft", str(OBSERVATION_DIR))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["outcome"] == "ready"
    assert payload["skipped_checks"] == [SKIPPED_11]
    assert payload["owc_commands"] == [
        f"iota-hub drafts owc {OBS_ID} --pick {OWC_A}",
        f"iota-hub drafts owc {OBS_ID} --pick {OWC_B}",
        f"iota-hub drafts owc {OBS_ID} --link <owc-url-or-id>",
        f"iota-hub drafts owc {OBS_ID} --none",
    ]
    assert "WARNING: 1 check did not run" in result.stderr


def test_a_settled_owc_link_is_not_warned_about(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    script_submit(
        mock_api,
        observation(readiness=ready(), checks=checks(), owc=ambiguous_owc("picked")),
    )

    result = run(runner, "submit", "--draft", str(OBSERVATION_DIR))

    assert result.exit_code == 0, result.output
    assert "WARNING" not in result.stderr


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (["--pick", OWC_A], {"owc_link_choice": "picked", "owc_event": OWC_A}),
        (
            ["--link", f"https://cloud.occultwatcher.net/event/{OWC_B}"],
            {
                "owc_link_choice": "pasted",
                "owc_event": f"https://cloud.occultwatcher.net/event/{OWC_B}",
            },
        ),
        (["--none"], {"owc_link_choice": "proceed_unlinked"}),
    ],
)
def test_drafts_owc_patches_the_choice_with_the_version_it_read(
    runner: CliRunner, mock_api: MockAPI, flags, expected
) -> None:
    mock_api.json("GET", GET, observation(owc=ambiguous_owc()))
    mock_api.json("PATCH", GET, observation(version=5, owc=ambiguous_owc("picked")))

    result = run(runner, "--json", "drafts", "owc", OBS_ID, *flags)

    assert result.exit_code == 0, result.output
    assert json.loads(mock_api.last_request.content) == {"version": 4, **expected}
    assert json.loads(result.stdout)["owc"]["choice"] == "picked"


@pytest.mark.parametrize("flags", [[], ["--pick", OWC_A, "--none"]])
def test_drafts_owc_needs_exactly_one_choice(
    runner: CliRunner, mock_api: MockAPI, flags
) -> None:
    result = run(runner, "drafts", "owc", OBS_ID, *flags)

    assert result.exit_code == 2
    assert mock_api.requests == []


def test_drafts_confirm_asteroid(runner: CliRunner, mock_api: MockAPI) -> None:
    mock_api.json("GET", GET, observation())
    mock_api.json("PATCH", GET, observation(version=5))

    result = run(runner, "drafts", "confirm-asteroid", OBS_ID)

    assert result.exit_code == 0, result.output
    assert json.loads(mock_api.last_request.content) == {
        "version": 4,
        "confirm_unnumbered_asteroid_id": True,
    }


def test_drafts_comment(runner: CliRunner, mock_api: MockAPI) -> None:
    mock_api.json("GET", GET, observation())
    mock_api.json("PATCH", GET, observation(version=5))

    result = run(runner, "drafts", "comment", OBS_ID, "Clouds after R.")

    assert result.exit_code == 0, result.output
    assert json.loads(mock_api.last_request.content) == {
        "version": 4,
        "comments": "Clouds after R.",
    }


def test_a_rejected_pasted_link_is_the_apis_code(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    mock_api.json("GET", GET, observation())
    mock_api.json(
        "PATCH",
        GET,
        {
            "code": "owc_link_mismatch",
            "message": "The OWC event you linked does not match your report.",
            "hint": "Link the right event.",
            "details": {"violations": [{"field": "asteroid_number"}]},
        },
        status=422,
    )

    result = run(runner, "--json", "drafts", "owc", OBS_ID, "--link", OWC_A)

    assert result.exit_code == 1
    error = json.loads(result.stderr[result.stderr.index("{") :])
    assert error["code"] == "owc_link_mismatch"


def test_show_renders_the_owc_state_and_the_skipped_checks(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    mock_api.json(
        "GET",
        GET,
        observation(checks=checks(skipped=[SKIPPED_11]), owc=ambiguous_owc()),
    )

    result = run(runner, "drafts", "show", OBS_ID)

    assert result.exit_code == 0, result.output
    assert "OWC:          unresolved (ambiguous)" in result.stdout
    assert OWC_A in result.stdout
    assert "Skipped (did not run):" in result.stdout


# -- the JSON report, the aperture record, check and amend ------------------

CHECK = f"{OBSERVATIONS}/check"
WEB_URL = f"https://hub.example.test/new-observation?draft={OBS_ID}"


def test_dry_run_maps_a_json_report_and_an_aperture_record(
    runner: CliRunner, tmp_path: Path
) -> None:
    folder = tmp_path / "obs"
    folder.mkdir()
    for source in (LIGHTCURVE, LOG):
        (folder / source.name).write_bytes(source.read_bytes())
    (folder / "20180305_9721_Doty_Observer+.json").write_text("{}")
    (folder / "X_Tangra.lc").write_bytes(b"lc")

    result = run(runner, "submit", str(folder), "--dry-run")
    assert result.exit_code == 0, result.stderr
    assert "report           20180305_9721_Doty_Observer+.json" in result.stdout
    assert "aperture_record  X_Tangra.lc" in result.stdout
    assert "attachment" not in result.stdout

    result = run(runner, "--json", "submit", str(folder), "--dry-run")
    payload = json.loads(result.stdout)
    assert {u["slot"]: u["filename"] for u in payload["uploads"]} == {
        "report": "20180305_9721_Doty_Observer+.json",
        "lightcurve": LIGHTCURVE.name,
        "log": LOG.name,
        "aperture_record": "X_Tangra.lc",
    }
    assert payload["attachments"] == []


def test_the_aperture_record_flag_names_a_file_from_elsewhere(
    runner: CliRunner, tmp_path: Path
) -> None:
    record = tmp_path / "frames.pymovie"
    record.write_bytes(b"PYMOVIE\x00")
    result = run(
        runner,
        "--json",
        "submit",
        str(OBSERVATION_DIR),
        "--dry-run",
        "--aperture-record",
        str(record),
    )
    assert result.exit_code == 0, result.stderr
    uploads = {u["slot"]: u["path"] for u in json.loads(result.stdout)["uploads"]}
    assert uploads["aperture_record"] == str(record)


def test_the_aperture_record_flag_checks_the_extension(
    runner: CliRunner, tmp_path: Path, mock_api: MockAPI
) -> None:
    wrong = tmp_path / "frames.fits"
    wrong.write_bytes(b"x")
    result = run(
        runner, "submit", str(OBSERVATION_DIR), "--aperture-record", str(wrong)
    )
    assert result.exit_code == 2
    assert "error: invalid_extension:" in result.stderr
    assert mock_api.requests == []


def test_needs_attention_prints_the_web_url(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    script_submit(
        mock_api,
        observation(
            web_url=WEB_URL,
            readiness=ready(state="needs_attention", open_findings=1),
            checks=checks(open_findings=1, findings=[finding()]),
            next_actions=["dismiss_or_fix"],
        ),
    )
    result = run(runner, "submit", str(OBSERVATION_DIR))
    assert result.exit_code == 4
    assert f"Web:          {WEB_URL}" in result.stdout


def test_a_no_wait_draft_prints_the_web_url_and_carries_it_in_json(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    script_submit(mock_api, observation(), finalized=observation(web_url=WEB_URL))
    result = run(runner, "submit", str(OBSERVATION_DIR), "--no-wait")
    assert result.exit_code == 0
    assert f"Web:          {WEB_URL}" in result.stdout

    result = run(runner, "--json", "submit", str(OBSERVATION_DIR), "--no-wait")
    payload = json.loads(result.stdout)
    assert payload["outcome"] == "draft"
    assert payload["web_url"] == WEB_URL


def test_a_submitted_outcome_does_not_print_the_web_url(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    script_submit(mock_api, observation(readiness=ready(), checks=checks()))
    mock_api.json(
        "POST",
        SUBMIT,
        observation(submission_status="submitted", status="submitted", web_url="x"),
    )
    result = run(runner, "submit", str(OBSERVATION_DIR))
    assert result.exit_code == 0
    assert "Web:" not in result.stdout


def test_observations_check_finds_your_own_draft(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    mock_api.json(
        "POST",
        CHECK,
        {
            "found": "observation",
            "observation": {
                "observation_id": OBS_ID,
                "status": "draft",
                "amendment_count": 0,
                "web_url": WEB_URL,
            },
            "event": None,
        },
    )
    result = run(
        runner,
        "observations",
        "check",
        "--asteroid-id",
        "9721",
        "--asteroid-name",
        "Doty",
        "--star-catalog",
        "UCAC4",
        "--star-id",
        "1234-56789",
        "--date",
        "2018-03-05",
        "--station",
        "2",
    )
    assert result.exit_code == 0, result.stderr
    assert "Found:        yes, one of yours" in result.stdout
    assert f"Observation:  {OBS_ID}" in result.stdout
    assert "Status:       draft" in result.stdout
    assert f"Web:          {WEB_URL}" in result.stdout
    assert f"iota-hub drafts show {OBS_ID}" in result.stdout
    assert json.loads(mock_api.last_request.content) == {
        "asteroid_id": "9721",
        "asteroid_name": "Doty",
        "star_catalog": "UCAC4",
        "star_id": "1234-56789",
        "observed_date": "2018-03-05",
        "station": "2",
    }


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"found": "none"}, ["Found:        no"]),
        (
            {"found": "event", "event": {"event_id": "evt_1", "status": "IN_REVIEW"}},
            [
                "Found:        no, but the event exists",
                "Event:        evt_1",
                "Event status: IN_REVIEW",
            ],
        ),
    ],
)
def test_observations_check_reports_an_event_or_nothing_and_exits_0(
    runner: CliRunner, mock_api: MockAPI, answer: dict, expected: list[str]
) -> None:
    mock_api.json("POST", CHECK, answer)
    result = run(
        runner,
        "observations",
        "check",
        "--asteroid-name",
        "Doty",
        "--star-id",
        "1-2",
        "--date",
        "2018-03-05",
    )
    assert result.exit_code == 0
    for line in expected:
        assert line in result.stdout
    assert "Observation:" not in result.stdout
    body = json.loads(mock_api.last_request.content)
    assert body["asteroid_id"] is None and body["station"] is None


def test_observations_check_json_is_the_api_answer(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    answer = {
        "found": "observation",
        "observation": {
            "observation_id": OBS_ID,
            "status": "amended",
            "amendment_count": 2,
            "web_url": f"https://hub.example.test/observations/{OBS_ID}",
        },
        "event": {"event_id": "evt_1", "status": "READY_FOR_REVIEW"},
    }
    mock_api.json("POST", CHECK, answer)
    result = run(
        runner,
        "--json",
        "observations",
        "check",
        "--asteroid-name",
        "Doty",
        "--star-id",
        "1-2",
        "--date",
        "2018-03-05",
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout) == answer


def test_observations_check_needs_the_identity(runner: CliRunner) -> None:
    result = run(runner, "observations", "check", "--asteroid-name", "Doty")
    assert result.exit_code == 2


def test_show_renders_the_derived_status_amendments_and_web_url(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    mock_api.json(
        "GET",
        GET,
        observation(
            submission_status="submitted",
            status="amended",
            amendment_count=2,
            amended_at="2026-02-01T00:00:00Z",
            web_url=f"https://hub.example.test/observations/{OBS_ID}",
        ),
    )
    result = run(runner, "observations", "show", OBS_ID)
    assert result.exit_code == 0
    assert "Status:       amended" in result.stdout
    assert "Amended:      2 time(s), last 2026-02-01T00:00:00Z" in result.stdout
    assert "Web:          https://hub.example.test/observations/obs_1" in result.stdout


def test_an_amend_with_another_identity_exits_2_with_the_changes(
    runner: CliRunner, mock_api: MockAPI
) -> None:
    changed = [{"field": "star_id", "stored": "1-2", "parsed": "1-3"}]
    mock_api.json(
        "POST", f"{GET}/files/report/upload/init", upload_target("report", REPORT.name)
    )
    mock_api.add("POST", BUCKET, httpx.Response(204))
    mock_api.json(
        "POST",
        f"{GET}/files/report/upload/finalize",
        {
            "code": "identity_changed",
            "message": "The replacement report describes a different observation",
            "hint": "Fix the report so it describes this observation.",
            "details": {"status": "submitted", "changed": changed},
        },
        status=422,
    )
    result = run(
        runner, "--json", "drafts", "files", "add", OBS_ID, "report", str(REPORT)
    )
    assert result.exit_code == 2
    error = json.loads(result.stderr[result.stderr.index("{") :])
    assert error["code"] == "identity_changed"
    assert error["details"]["changed"] == changed
    assert error["exit_code"] == 2


def test_an_amend_after_the_lock_exits_1(runner: CliRunner, mock_api: MockAPI) -> None:
    mock_api.json(
        "POST",
        f"{GET}/files/lightcurve/upload/init",
        {
            "code": "already_processed",
            "message": "The event is in review",
            "hint": "Contact a coordinator if a correction is needed.",
            "details": {"status": "locked", "observation_id": OBS_ID},
        },
        status=409,
    )
    result = run(
        runner, "drafts", "files", "add", OBS_ID, "lightcurve", str(LIGHTCURVE)
    )
    assert result.exit_code == 1
    assert "error: already_processed:" in result.stderr
    assert "hint: Contact a coordinator" in result.stderr


def test_drafts_files_add_takes_the_aperture_record_slot(
    runner: CliRunner, mock_api: MockAPI, tmp_path: Path
) -> None:
    record = tmp_path / "X.pymovie"
    record.write_bytes(b"PYMOVIE\x00")
    mock_api.json(
        "POST",
        f"{GET}/files/aperture_record/upload/init",
        upload_target("aperture_record", record.name),
    )
    mock_api.add("POST", BUCKET, httpx.Response(204))
    mock_api.json("POST", f"{GET}/files/aperture_record/upload/finalize", observation())
    result = run(
        runner, "drafts", "files", "add", OBS_ID, "aperture_record", str(record)
    )
    assert result.exit_code == 0, result.stderr


# -- styled output (conventions section 13) -----------------------------------
#
# The tests above run colourless (NO_COLOR, not a TTY): the plain path. These
# force the styled one with a rich console that believes it is a terminal, and
# check the switch: pipes, NO_COLOR and --json stay plain, and a non-UTF-8
# console gets ASCII glyphs.


class _Terminal(io.StringIO):
    """A captured stream that reports a given encoding."""

    def __init__(self, encoding: str) -> None:
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._encoding

    def isatty(self) -> bool:
        return True


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch):
    """Make stdout and stderr styled terminals; returns their captured text."""

    def make(encoding: str = "utf-8") -> dict[bool, _Terminal]:
        streams = {False: _Terminal(encoding), True: _Terminal(encoding)}
        consoles = {
            err: Console(
                file=stream, force_terminal=True, color_system="standard", width=100
            )
            for err, stream in streams.items()
        }
        monkeypatch.setattr(
            _term,
            "console",
            lambda err=False: None if _term.plain else consoles[err],
        )
        return streams

    return make


def _needs_attention() -> dict:
    return observation(
        readiness=ready(state="needs_attention", open_findings=2),
        checks=checks(
            open_findings=2,
            findings=[
                finding(message="Report D is 5 s from the log."),
                finding(message="Report R is 6 s from the log.", fingerprint="7b3e"),
            ],
        ),
        next_actions=["dismiss_or_fix"],
    )


def test_styled_findings_say_a_shared_fix_once(
    runner: CliRunner, mock_api, terminal
) -> None:
    streams = terminal()
    script_submit(mock_api, _needs_attention())
    result = run(runner, "submit", str(OBSERVATION_DIR))
    assert result.exit_code == 4

    out = streams[False].getvalue()
    assert "\x1b[" in out  # styled
    assert "lightcurve_time_gap (2)" in out
    assert out.count("Re-export the light curve") == 1
    assert "9f2a1c, 7b3e" in out
    assert "Report D is 5 s" in out and "Report R is 6 s" in out
    assert f"iota-hub drafts dismiss {OBS_ID} 7b3e" in out
    # Readiness is not repeated when it says what the outcome already said.
    assert "Readiness" not in out
    # Progress went to the stderr terminal, not stdout.
    assert "Creating draft" in streams[True].getvalue()
    assert "Creating draft" not in out


def test_styled_glyphs_need_utf8(runner: CliRunner, mock_api, terminal) -> None:
    streams = terminal("cp1252")
    script_submit(mock_api, observation(readiness=ready(), checks=checks()))
    mock_api.json(
        "POST",
        f"/public/v1/observations/{OBS_ID}/submit",
        observation(submission_status="submitted", status="submitted"),
    )
    result = run(runner, "submit", str(OBSERVATION_DIR))
    assert result.exit_code == 0

    out = streams[False].getvalue()
    assert "ok submitted" in out
    (out + streams[True].getvalue()).encode("ascii")


def test_json_is_never_styled(runner: CliRunner, mock_api, terminal) -> None:
    streams = terminal()
    script_submit(mock_api, _needs_attention())
    result = run(runner, "--json", "submit", str(OBSERVATION_DIR))
    assert result.exit_code == 4
    assert streams[False].getvalue() == ""
    assert "\x1b[" not in result.stdout


@pytest.mark.parametrize(
    ("tty", "no_color", "plain", "expected"),
    [
        (True, None, False, True),
        (False, None, False, False),  # piped
        (True, "1", False, False),  # NO_COLOR
        (True, None, True, False),  # --json
    ],
)
def test_styled_only_on_a_colour_terminal(
    monkeypatch: pytest.MonkeyPatch, tty, no_color, plain, expected
) -> None:
    stream = _Terminal("utf-8")
    monkeypatch.setattr(stream, "isatty", lambda: tty)
    monkeypatch.setattr("sys.stdout", stream)
    if no_color is None:
        monkeypatch.delenv("NO_COLOR", raising=False)
    else:
        monkeypatch.setenv("NO_COLOR", no_color)
    monkeypatch.setattr(_term, "plain", plain)
    assert _term.styled() is expected


def test_a_repeated_progress_line_prints_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_module._LAST_PROGRESS[:] = []
    for message in ["Waiting for checks...", "Waiting for checks...", "Done"]:
        cli_module._progress(message)
    assert capsys.readouterr().err == "Waiting for checks...\nDone\n"


def test_styled_urls_are_never_broken(runner: CliRunner, mock_api, terminal) -> None:
    """A URL wider than the terminal stays one string the terminal can link."""
    streams = terminal()
    long_url = "https://example.test/observations/" + "x" * 120
    mock_api.json(
        "GET", f"/public/v1/observations/{OBS_ID}", observation(web_url=long_url)
    )
    result = run(runner, "observations", "show", OBS_ID)
    assert result.exit_code == 0
    assert long_url in streams[False].getvalue()


def test_a_table_row_wider_than_the_terminal_stays_one_line(
    runner: CliRunner, mock_api: MockAPI, terminal
) -> None:
    # rich must not break a row at the width it saw: those breaks are hard
    # newlines that never reflow when the window is widened. The terminal
    # soft-wraps the line instead.
    streams = terminal()
    name = "Asteroid " + "x" * 80
    mock_api.json(
        "GET",
        EVENTS,
        {"items": [event("evt_1", asteroid_name=name)], "next_cursor": None},
    )
    result = run(runner, "events", "list")
    assert result.exit_code == 0
    lines = streams[False].getvalue().splitlines()
    assert len(lines) == 2
    assert lines[1].startswith(("evt_1", "\x1b")) and name in lines[1]


def test_a_path_with_a_space_is_printed_pasteable() -> None:
    spaced = Path("/Users/x/Library/Application Support/iota-hub/config.toml")
    assert cli_module._pasteable(spaced) == f'"{spaced}"'
    plain = Path("/home/x/.config/iota-hub/config.toml")
    assert cli_module._pasteable(plain) == str(plain)


@pytest.mark.parametrize(
    ("size", "shown"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (64275, "62.8 KB"),
        (5 * 1024 * 1024, "5.0 MB"),
    ],
)
def test_sizes_read_like_the_web_app(size: int, shown: str) -> None:
    assert cli_module._size(size) == shown
