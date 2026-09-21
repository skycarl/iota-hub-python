"""The workflow layer: submit, poll, re-upload, download, next actions."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from conftest import FIXTURES, MockAPI, body_of

from iota_hub import IotaHubError, next_actions_commands, sha256_base64
from iota_hub.models import PublicObservation

OBSERVATION_DIR = FIXTURES / "observation"
REPORT = OBSERVATION_DIR / "20180305_9721_Doty_Observer_POS.xlsx"
LIGHTCURVE = OBSERVATION_DIR / "20180305_9721_Doty_Observer_POS.csv"
LOG = OBSERVATION_DIR / "20180305_9721_Doty_Observer_POS_pyote_log.txt"
FILES = {"report": REPORT, "lightcurve": LIGHTCURVE, "log": LOG}

OBS_ID = "obs_1"
DRAFTS = "/public/v1/observations/drafts"
FINALIZE = f"/public/v1/observations/{OBS_ID}/finalize"
GET = f"/public/v1/observations/{OBS_ID}"
SUBMIT = f"/public/v1/observations/{OBS_ID}/submit"
BUCKET = "/staging-bucket"

UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


# -- scripting the API ------------------------------------------------------


def observation(**overrides):
    body = {
        "observation_id": OBS_ID,
        "submission_status": "draft",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "version": 4,
        "observer_user_id": "user_1",
        "metadata": {"observer_name": "Test Observer"},
        "next_actions": ["poll"],
    }
    body.update(overrides)
    return body


def readiness(state="ready", **overrides):
    body = {"state": state, "checks_current": True}
    body.update(overrides)
    return body


def upload_target(slot, filename, *, url=f"https://s3.example.test{BUCKET}", key=None):
    file_key = key or f"staging/{OBS_ID}/{slot}/{filename}"
    return {
        "slot": slot,
        "filename": filename,
        "upload_url": url,
        # Deliberately more than one field, so "in the order returned" and
        # "the file part last" are assertable.
        "fields": {"key": file_key, "policy": "b64policy", "x-amz-signature": "sig"},
        "file_key": file_key,
    }


def script_create(mock_api: MockAPI, *, slots=("report", "lightcurve", "log")):
    mock_api.json(
        "POST",
        DRAFTS,
        {
            "observation": observation(next_actions=[]),
            "uploads": [upload_target(slot, FILES[slot].name) for slot in slots],
        },
        status=201,
    )


def script_upload(mock_api: MockAPI, *responses, path=BUCKET):
    mock_api.add("POST", path, *(responses or (httpx.Response(204),)))


def form_field_names(request: httpx.Request) -> list[str]:
    """The multipart part names, in the order they were written.

    The lookbehind keeps ``filename="…"`` out of the list.
    """
    return re.findall(r'(?<!file)name="([^"]+)"', request.content.decode("latin-1"))


def requests_to(mock_api: MockAPI, path: str) -> list[httpx.Request]:
    return [r for r in mock_api.requests if r.url.path == path]


# -- the whole job ----------------------------------------------------------


def test_submit_files_creates_uploads_finalizes_waits_and_submits(
    client, mock_api, sleeps
):
    script_create(mock_api)
    script_upload(mock_api)
    mock_api.json(
        "POST", FINALIZE, observation(checks={"run_id": "r1", "status": "running"})
    )
    mock_api.json(
        "GET",
        GET,
        observation(checks={"run_id": "r1", "status": "running"}),
        status=200,
    )
    mock_api.json("GET", GET, observation(checks={"run_id": "r1", "status": "pending"}))
    mock_api.json(
        "GET",
        GET,
        observation(
            checks={"run_id": "r1", "status": "complete", "open_findings": 0},
            readiness=readiness(),
            next_actions=["submit"],
        ),
    )
    mock_api.json(
        "POST",
        SUBMIT,
        observation(
            submission_status="submitted",
            submitted_at="2026-01-02T00:00:00Z",
            version=5,
            next_actions=[],
        ),
    )

    messages: list[str] = []
    result = client.submit_files(FILES, on_progress=messages.append)

    assert result.outcome == "submitted"
    assert result.submitted is True
    assert result.observation.submission_status == "submitted"
    assert result.uploads == {
        "report": REPORT.name,
        "lightcurve": LIGHTCURVE.name,
        "log": LOG.name,
    }

    # Every file is declared with its real size and digest.
    declared = body_of(requests_to(mock_api, DRAFTS)[0])["files"]
    assert [item["slot"] for item in declared] == ["report", "lightcurve", "log"]
    assert declared[1] == {
        "slot": "lightcurve",
        "filename": LIGHTCURVE.name,
        "size": LIGHTCURVE.stat().st_size,
        "sha256": sha256_base64(LIGHTCURVE),
    }

    # The S3 posts carry the signed fields in order, the file last, no key.
    posts = requests_to(mock_api, BUCKET)
    assert len(posts) == 3
    for post in posts:
        assert form_field_names(post) == ["key", "policy", "x-amz-signature", "file"]
        assert "X-API-Key" not in post.headers

    # One uuid4 idempotency key per logical write, never reused.
    keys = [
        request.headers["Idempotency-Key"]
        for request in mock_api.requests
        if "Idempotency-Key" in request.headers
    ]
    assert len(keys) == 3  # create, finalize, submit
    assert all(UUID4.match(key) for key in keys)
    assert len(set(keys)) == 3

    # 2 s, then x1.5, and no sleep after the terminal poll.
    assert sleeps == [2.0, 3.0]
    assert body_of(requests_to(mock_api, SUBMIT)[0]) == {"version": 4}
    assert messages[0].startswith("Creating draft")
    assert "Uploading lightcurve (2/3)..." in messages
    assert "Waiting for checks..." in messages


def test_findings_are_never_submitted(client, mock_api):
    script_create(mock_api)
    script_upload(mock_api)
    mock_api.json("POST", FINALIZE, observation())
    mock_api.json(
        "GET",
        GET,
        observation(
            checks={
                "run_id": "r1",
                "status": "complete",
                "open_findings": 1,
                "findings": [
                    {
                        "check_number": 3,
                        "code": "star_mismatch",
                        "name": "Star mismatch",
                        "severity": "error",
                        "message": "The star id does not match the event.",
                        "fingerprint": "fp_1",
                    }
                ],
            },
            readiness=readiness("needs_attention", open_findings=1),
            next_actions=["dismiss_or_fix"],
        ),
    )

    result = client.submit_files(FILES)

    assert result.outcome == "needs_attention"
    assert result.submitted is False
    assert requests_to(mock_api, SUBMIT) == []
    assert result.observation.checks.findings[0].fingerprint == "fp_1"


def test_missing_required_is_needs_attention_too(client, mock_api):
    script_create(mock_api)
    script_upload(mock_api)
    mock_api.json("POST", FINALIZE, observation())
    mock_api.json(
        "GET",
        GET,
        observation(
            checks={"run_id": "r1", "status": "complete"},
            readiness=readiness("in_progress", missing_required=["star"]),
            next_actions=["upload:report"],
        ),
    )

    result = client.submit_files(FILES)

    assert result.outcome == "needs_attention"
    assert requests_to(mock_api, SUBMIT) == []


def test_submit_false_stops_at_ready(client, mock_api):
    script_create(mock_api)
    script_upload(mock_api)
    mock_api.json("POST", FINALIZE, observation())
    mock_api.json(
        "GET",
        GET,
        observation(
            checks={"run_id": "r1", "status": "complete"},
            readiness=readiness(),
            next_actions=["submit"],
        ),
    )

    result = client.submit_files(FILES, submit=False)

    assert result.outcome == "ready"
    assert result.submitted is False
    assert requests_to(mock_api, SUBMIT) == []


def test_wait_false_returns_the_finalized_draft(client, mock_api, sleeps):
    script_create(mock_api)
    script_upload(mock_api)
    mock_api.json(
        "POST", FINALIZE, observation(checks={"run_id": "r1", "status": "running"})
    )

    result = client.submit_files(FILES, wait=False)

    assert result.outcome == "draft"
    assert result.observation_id == OBS_ID
    assert requests_to(mock_api, GET) == []
    assert sleeps == []


def test_a_server_side_auto_submit_is_reported_as_submitted(client, mock_api):
    script_create(mock_api)
    script_upload(mock_api)
    mock_api.json("POST", FINALIZE, observation())
    mock_api.json(
        "GET",
        GET,
        observation(
            submission_status="submitted",
            checks={"run_id": "r1", "status": "complete"},
            next_actions=[],
        ),
    )

    result = client.submit_files(FILES, auto_submit_when_clean=True)

    assert body_of(requests_to(mock_api, DRAFTS)[0])["auto_submit_when_clean"] is True
    assert result.outcome == "submitted"
    assert result.submitted is True
    assert requests_to(mock_api, SUBMIT) == []


def test_submit_folder_reports_what_it_did_not_upload(client, mock_api, tmp_path):
    folder = tmp_path / "obs"
    folder.mkdir()
    for source in FILES.values():
        (folder / source.name).write_bytes(source.read_bytes())
    (folder / "_notes.txt").write_text("for the reviewer\n")

    script_create(mock_api)
    script_upload(mock_api)
    mock_api.json("POST", FINALIZE, observation())
    mock_api.json(
        "GET",
        GET,
        observation(
            checks={"run_id": "r1", "status": "complete"},
            readiness=readiness(),
        ),
    )
    mock_api.json("POST", SUBMIT, observation(submission_status="submitted"))

    result = client.submit_folder(folder)

    assert result.outcome == "submitted"
    assert [path.name for path in result.ignored] == ["_notes.txt"]


# -- polling ----------------------------------------------------------------


def test_wait_for_checks_times_out(client, mock_api, sleeps):
    mock_api.json("GET", GET, observation(checks={"run_id": "r1", "status": "running"}))

    with pytest.raises(IotaHubError) as excinfo:
        client.wait_for_checks(OBS_ID, timeout=5.0)

    error = excinfo.value
    assert error.code == "timeout"
    assert error.details["observation_id"] == OBS_ID
    assert error.details["checks_status"] == "running"
    # The last wait is trimmed to what is left of the timeout.
    assert sleeps == [2.0, 3.0]
    assert len(requests_to(mock_api, GET)) == 3


def test_wait_for_checks_stops_when_there_is_nothing_to_poll(client, mock_api, sleeps):
    mock_api.json(
        "GET",
        GET,
        observation(
            readiness=readiness("in_progress", missing_required=["report"]),
            next_actions=["upload:report"],
        ),
    )

    result = client.wait_for_checks(OBS_ID)

    assert result.checks is None
    assert sleeps == []


def test_wait_for_checks_ends_on_an_unknown_terminal_status(client, mock_api):
    mock_api.json(
        "GET", GET, observation(checks={"run_id": "r1", "status": "cancelled"})
    )

    assert client.wait_for_checks(OBS_ID).checks.status == "cancelled"


# -- upload failures --------------------------------------------------------


SECOND_BUCKET = "/retry-bucket"
INIT_LIGHTCURVE = f"/public/v1/observations/{OBS_ID}/files/lightcurve/upload/init"
FINALIZE_LIGHTCURVE = (
    f"/public/v1/observations/{OBS_ID}/files/lightcurve/upload/finalize"
)


def script_expired_lightcurve(mock_api: MockAPI):
    script_create(mock_api)
    # report succeeds, lightcurve's target has expired, log succeeds.
    script_upload(
        mock_api,
        httpx.Response(204),
        httpx.Response(403, text="<Error>AccessDenied</Error>"),
        httpx.Response(204),
    )
    mock_api.json(
        "POST",
        INIT_LIGHTCURVE,
        upload_target(
            "lightcurve",
            LIGHTCURVE.name,
            url=f"https://s3.example.test{SECOND_BUCKET}",
            key=f"staging/{OBS_ID}/lightcurve/retry.csv",
        ),
    )


def test_an_expired_target_is_reinitialized_once_and_committed_per_slot(
    client, mock_api
):
    script_expired_lightcurve(mock_api)
    script_upload(mock_api, httpx.Response(204), path=SECOND_BUCKET)
    mock_api.json("POST", FINALIZE_LIGHTCURVE, observation())
    mock_api.json("POST", FINALIZE, observation())
    mock_api.json(
        "GET",
        GET,
        observation(
            checks={"run_id": "r1", "status": "complete"}, readiness=readiness()
        ),
    )
    mock_api.json("POST", SUBMIT, observation(submission_status="submitted"))

    result = client.submit_files(FILES)

    assert result.outcome == "submitted"
    assert len(requests_to(mock_api, INIT_LIGHTCURVE)) == 1
    assert body_of(requests_to(mock_api, INIT_LIGHTCURVE)[0]) == {
        "filename": LIGHTCURVE.name,
        "sha256": sha256_base64(LIGHTCURVE),
    }
    # The per-slot finalize is what drops the slot from ``pending_uploads``;
    # without it the collapsed finalize would answer ``upload_missing``.
    assert body_of(requests_to(mock_api, FINALIZE_LIGHTCURVE)[0]) == {
        "file_key": f"staging/{OBS_ID}/lightcurve/retry.csv",
        "filename": LIGHTCURVE.name,
    }


def test_a_second_upload_failure_raises_and_leaves_the_draft(client, mock_api):
    script_expired_lightcurve(mock_api)
    script_upload(mock_api, httpx.Response(403), path=SECOND_BUCKET)

    with pytest.raises(IotaHubError) as excinfo:
        client.submit_files(FILES)

    assert excinfo.value.code == "upload_failed"
    assert excinfo.value.details["slot"] == "lightcurve"
    assert len(requests_to(mock_api, INIT_LIGHTCURVE)) == 1
    # Nothing was finalized, and the draft was not deleted for the caller.
    assert requests_to(mock_api, FINALIZE) == []
    assert requests_to(mock_api, GET) == []


# -- the fix loop -----------------------------------------------------------


def test_replace_file_inits_uploads_and_finalizes(client, mock_api):
    mock_api.json("POST", INIT_LIGHTCURVE, upload_target("lightcurve", LIGHTCURVE.name))
    script_upload(mock_api)
    mock_api.json("POST", FINALIZE_LIGHTCURVE, observation(version=6))

    result = client.replace_file(OBS_ID, "lightcurve", LIGHTCURVE)

    assert result.version == 6
    assert body_of(requests_to(mock_api, INIT_LIGHTCURVE)[0])["sha256"] == (
        sha256_base64(LIGHTCURVE)
    )
    assert form_field_names(requests_to(mock_api, BUCKET)[0])[-1] == "file"
    assert UUID4.match(
        requests_to(mock_api, FINALIZE_LIGHTCURVE)[0].headers["Idempotency-Key"]
    )


# -- downloads --------------------------------------------------------------


FILE_LIST = f"/public/v1/observations/{OBS_ID}/files"
REPORT_URL = "/download/report.xlsx"
CURVE_URL = "/download/curve.csv"


def script_file_list(mock_api: MockAPI):
    mock_api.json(
        "GET",
        FILE_LIST,
        {
            "observation_id": OBS_ID,
            "expires_in": 900,
            "files": [
                {
                    "slot": "report",
                    "filename": "report.xlsx",
                    "size_bytes": 12,
                    "url": f"https://s3.example.test{REPORT_URL}?sig=1",
                    "expires_in": 900,
                },
                {
                    "slot": "lightcurve",
                    "filename": "curve.csv",
                    "size_bytes": 4,
                    "url": f"https://s3.example.test{CURVE_URL}?sig=2",
                    "expires_in": 900,
                },
            ],
        },
    )


def test_download_files_writes_every_file_without_the_key(client, mock_api, tmp_path):
    script_file_list(mock_api)
    mock_api.add("GET", REPORT_URL, httpx.Response(200, content=b"report-bytes"))
    mock_api.add("GET", CURVE_URL, httpx.Response(200, content=b"t,v\n"))

    written = client.download_files("observation", OBS_ID, tmp_path / "out")

    assert [path.name for path in written] == ["report.xlsx", "curve.csv"]
    assert (tmp_path / "out" / "report.xlsx").read_bytes() == b"report-bytes"
    assert (tmp_path / "out" / "curve.csv").read_text() == "t,v\n"
    for request in requests_to(mock_api, REPORT_URL) + requests_to(mock_api, CURVE_URL):
        assert "X-API-Key" not in request.headers


def test_download_files_honours_the_slot_filter(client, mock_api, tmp_path):
    script_file_list(mock_api)
    mock_api.add("GET", CURVE_URL, httpx.Response(200, content=b"t,v\n"))

    written = client.download_files("observation", OBS_ID, tmp_path, slot="lightcurve")

    assert [path.name for path in written] == ["curve.csv"]
    assert requests_to(mock_api, REPORT_URL) == []


def test_download_files_refuses_to_overwrite(client, mock_api, tmp_path):
    script_file_list(mock_api)
    (tmp_path / "report.xlsx").write_bytes(b"mine")

    with pytest.raises(IotaHubError) as excinfo:
        client.download_files("observation", OBS_ID, tmp_path)

    assert excinfo.value.code == "file_exists"
    # Nothing was fetched: the check happens before the first byte is written.
    assert requests_to(mock_api, CURVE_URL) == []
    assert (tmp_path / "report.xlsx").read_bytes() == b"mine"


def test_download_files_overwrites_when_told_to(client, mock_api, tmp_path):
    script_file_list(mock_api)
    mock_api.add("GET", REPORT_URL, httpx.Response(200, content=b"fresh"))
    mock_api.add("GET", CURVE_URL, httpx.Response(200, content=b"t,v\n"))
    (tmp_path / "report.xlsx").write_bytes(b"stale")

    client.download_files("observation", OBS_ID, tmp_path, overwrite=True)

    assert (tmp_path / "report.xlsx").read_bytes() == b"fresh"


@pytest.mark.parametrize("filename", ["..", ".", "", "sub/.."])
def test_a_filename_that_names_no_file_is_refused(client, mock_api, tmp_path, filename):
    mock_api.json(
        "GET",
        FILE_LIST,
        {
            "observation_id": OBS_ID,
            "expires_in": 900,
            "files": [
                {
                    "slot": "report",
                    "filename": filename,
                    "size_bytes": 12,
                    "url": f"https://s3.example.test{REPORT_URL}?sig=1",
                    "expires_in": 900,
                }
            ],
        },
    )

    with pytest.raises(IotaHubError) as excinfo:
        client.download_files("observation", OBS_ID, tmp_path)

    assert excinfo.value.code == "download_failed"
    # Refused before any request: nothing was fetched and nothing was written.
    assert requests_to(mock_api, REPORT_URL) == []
    assert list(tmp_path.iterdir()) == []


def test_an_expired_link_is_download_failed(client, mock_api, tmp_path):
    script_file_list(mock_api)
    mock_api.add("GET", REPORT_URL, httpx.Response(403, text="<Error>Expired</Error>"))

    with pytest.raises(IotaHubError) as excinfo:
        client.download_files("observation", OBS_ID, tmp_path)

    assert excinfo.value.code == "download_failed"
    assert excinfo.value.status == 403


def test_download_files_reads_event_files_too(client, mock_api, tmp_path):
    mock_api.json(
        "GET",
        "/public/v1/events/evt_1/files",
        {
            "event_id": "evt_1",
            "expires_in": 900,
            "files": [
                {
                    "slot": "damit",
                    "filename": "model.txt",
                    "size_bytes": 5,
                    "url": f"https://s3.example.test{REPORT_URL}",
                    "expires_in": 900,
                }
            ],
        },
    )
    mock_api.add("GET", REPORT_URL, httpx.Response(200, content=b"damit"))

    written = client.download_files("event", "evt_1", tmp_path)

    assert written == [tmp_path / "model.txt"]


def test_download_files_rejects_an_unknown_kind(client, tmp_path):
    with pytest.raises(ValueError, match="observation"):
        client.download_files("draft", OBS_ID, tmp_path)


# -- next_actions -----------------------------------------------------------


def with_actions(*verbs, checks=None) -> PublicObservation:
    return PublicObservation.model_validate(
        observation(next_actions=list(verbs), checks=checks)
    )


def test_every_verb_becomes_the_command_that_answers_it():
    assert next_actions_commands(with_actions("upload:report")) == [
        f"iota-hub drafts files add {OBS_ID} report <path>"
    ]
    assert next_actions_commands(with_actions("upload:lightcurve", "upload:log")) == [
        f"iota-hub drafts files add {OBS_ID} lightcurve <path>",
        f"iota-hub drafts files add {OBS_ID} log <path>",
    ]
    assert next_actions_commands(with_actions("run_checks")) == [
        f"iota-hub drafts check {OBS_ID}"
    ]
    assert next_actions_commands(with_actions("poll")) == [
        f"iota-hub drafts show {OBS_ID}"
    ]
    assert next_actions_commands(with_actions("submit")) == [
        f"iota-hub drafts submit {OBS_ID}"
    ]


def test_dismiss_or_fix_lists_one_command_per_open_finding():
    checks = {
        "run_id": "r1",
        "status": "complete",
        "open_findings": 2,
        "findings": [
            {
                "check_number": 1,
                "code": "a",
                "name": "A",
                "severity": "error",
                "message": "m",
                "fingerprint": "fp_1",
            },
            {
                "check_number": 2,
                "code": "b",
                "name": "B",
                "severity": "warning",
                "message": "m",
                "fingerprint": "fp_2",
            },
            {
                "check_number": 3,
                "code": "c",
                "name": "C",
                "severity": "warning",
                "message": "m",
                "fingerprint": "fp_3",
                "dismissal": {
                    "dismissed_by": "user_1",
                    "dismissed_at": "2026-01-01T00:00:00Z",
                    "note": "known",
                },
            },
        ],
    }

    commands = next_actions_commands(with_actions("dismiss_or_fix", checks=checks))

    assert commands[0] == (
        f'iota-hub drafts dismiss {OBS_ID} fp_1 --note "why this is expected"'
    )
    assert commands[1] == (
        f'iota-hub drafts dismiss {OBS_ID} fp_2 --note "why this is expected"'
    )
    # The dismissed finding is not offered again, and fixing is the other way.
    assert len(commands) == 3
    assert "files add" in commands[2]


def test_web_only_verbs_say_so_and_unknown_verbs_point_at_the_guide():
    for verb in ("confirm_asteroid_id", "resolve_event_files_conflict"):
        (command,) = next_actions_commands(with_actions(verb))
        assert "web app" in command

    assert next_actions_commands(with_actions("teleport")) == [
        "teleport: see iota-hub guide"
    ]


def test_the_commands_are_plain_ascii():
    """A non-UTF-8 Windows console has to survive everything we print."""
    for command in next_actions_commands(
        with_actions("poll", "confirm_asteroid_id", "resolve_event_files_conflict")
    ):
        command.encode("ascii")


def test_progress_messages_are_plain_ascii(client, mock_api):
    script_create(mock_api)
    script_upload(mock_api)
    mock_api.json("POST", FINALIZE, observation())
    mock_api.json(
        "GET", GET, observation(checks={"run_id": "r1", "status": "complete"})
    )

    messages: list[str] = []
    client.submit_files(FILES, on_progress=messages.append)

    assert messages
    for message in messages:
        message.encode("ascii")


def test_unknown_slots_are_rejected_before_anything_is_sent(client, mock_api):
    with pytest.raises(ValueError, match="attachment"):
        client.submit_files({"attachment": Path("notes.txt")})

    assert mock_api.requests == []
