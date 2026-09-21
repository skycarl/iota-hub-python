"""One test per ``operation_id``: method, path, body, query, response model."""

from __future__ import annotations

import json

import httpx
from conftest import SPEC_PATH, body_of

from iota_hub import Client, models
from iota_hub.models import PublicDeclaredFile

OBSERVATION = {
    "observation_id": "obs_1",
    "submission_status": "draft",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
    "version": 3,
    "observer_user_id": "user_1",
    "metadata": {"observer_name": "Test Observer"},
    "next_actions": ["upload:lightcurve"],
}
CHECKS = {"run_id": "run_1", "status": "running"}


def sent(mock_api):
    return mock_api.last_request


# -- writes -----------------------------------------------------------------


def test_create_draft(client, mock_api):
    mock_api.json(
        "POST",
        "/public/v1/observations/drafts",
        {
            "observation": OBSERVATION,
            "uploads": [
                {
                    "slot": "report",
                    "filename": "report.xlsx",
                    "upload_url": "https://s3.example.test/bucket",
                    "fields": {"key": "staging/obs_1/report/report.xlsx"},
                    "file_key": "staging/obs_1/report/report.xlsx",
                }
            ],
        },
        status=201,
    )
    created = client.create_draft(
        [PublicDeclaredFile(slot="report", filename="report.xlsx", size=51234)],
        auto_submit_when_clean=True,
        idempotency_key="key-1",
    )

    request = sent(mock_api)
    assert request.method == "POST"
    assert request.url.path == "/public/v1/observations/drafts"
    assert request.headers["Idempotency-Key"] == "key-1"
    assert body_of(request) == {
        "files": [
            {
                "slot": "report",
                "filename": "report.xlsx",
                "size": 51234,
                "sha256": None,
            }
        ],
        "auto_submit_when_clean": True,
    }
    assert isinstance(created, models.PublicDraftCreated)
    assert created.uploads[0].fields == {"key": "staging/obs_1/report/report.xlsx"}


def test_create_draft_accepts_plain_dicts_and_no_files(client, mock_api):
    mock_api.json(
        "POST",
        "/public/v1/observations/drafts",
        {"observation": OBSERVATION},
        status=201,
    )
    client.create_draft([{"slot": "log", "filename": "a_log.txt", "size": 10}])
    assert body_of(sent(mock_api))["files"][0]["slot"] == "log"

    client.create_draft()
    assert body_of(sent(mock_api)) == {"files": [], "auto_submit_when_clean": False}


def test_finalize_draft(client, mock_api):
    mock_api.json("POST", "/public/v1/observations/obs_1/finalize", OBSERVATION)
    observation = client.finalize_draft("obs_1", idempotency_key="key-2")

    request = sent(mock_api)
    assert (request.method, request.url.path) == (
        "POST",
        "/public/v1/observations/obs_1/finalize",
    )
    assert request.content == b""
    assert request.headers["Idempotency-Key"] == "key-2"
    assert isinstance(observation, models.PublicObservation)
    assert observation.version == 3


def test_init_file_upload(client, mock_api):
    mock_api.json(
        "POST",
        "/public/v1/observations/obs_1/files/lightcurve/upload/init",
        {
            "slot": "lightcurve",
            "filename": "lightcurve.csv",
            "upload_url": "https://s3.example.test/bucket",
            "fields": {"key": "staging/obs_1/lightcurve/lightcurve.csv"},
            "file_key": "staging/obs_1/lightcurve/lightcurve.csv",
        },
    )
    target = client.init_file_upload(
        "obs_1", "lightcurve", filename="lightcurve.csv", sha256="abc="
    )

    assert body_of(sent(mock_api)) == {"filename": "lightcurve.csv", "sha256": "abc="}
    assert isinstance(target, models.PublicUploadTarget)


def test_finalize_file_upload(client, mock_api):
    mock_api.json(
        "POST",
        "/public/v1/observations/obs_1/files/lightcurve/upload/finalize",
        OBSERVATION,
    )
    observation = client.finalize_file_upload(
        "obs_1", "lightcurve", file_key="staging/k", filename="lightcurve.csv"
    )

    assert body_of(sent(mock_api)) == {
        "file_key": "staging/k",
        "filename": "lightcurve.csv",
    }
    assert isinstance(observation, models.PublicObservation)


def test_delete_file(client, mock_api):
    mock_api.json("DELETE", "/public/v1/observations/obs_1/files/vizier", OBSERVATION)
    observation = client.delete_file("obs_1", "vizier")

    request = sent(mock_api)
    assert (request.method, request.url.path) == (
        "DELETE",
        "/public/v1/observations/obs_1/files/vizier",
    )
    assert isinstance(observation, models.PublicObservation)


def test_run_checks(client, mock_api):
    mock_api.json(
        "POST", "/public/v1/observations/obs_1/validation/runs", CHECKS, status=202
    )
    checks = client.run_checks("obs_1")

    assert sent(mock_api).url.path == "/public/v1/observations/obs_1/validation/runs"
    assert isinstance(checks, models.PublicChecks)
    assert checks.status == "running"


def test_dismiss_finding(client, mock_api):
    path = "/public/v1/observations/obs_1/validation/findings/fp_1/dismiss"
    mock_api.json("POST", path, CHECKS)
    checks = client.dismiss_finding("obs_1", "fp_1", note="Known header typo.")

    request = sent(mock_api)
    assert (request.method, request.url.path) == ("POST", path)
    assert body_of(request) == {"note": "Known header typo."}
    assert isinstance(checks, models.PublicChecks)


def test_undo_dismissal(client, mock_api):
    path = "/public/v1/observations/obs_1/validation/findings/fp_1/dismiss"
    mock_api.json("DELETE", path, CHECKS)
    checks = client.undo_dismissal("obs_1", "fp_1")

    assert (sent(mock_api).method, sent(mock_api).url.path) == ("DELETE", path)
    assert isinstance(checks, models.PublicChecks)


def test_submit_draft(client, mock_api):
    mock_api.json("POST", "/public/v1/observations/obs_1/submit", OBSERVATION)
    observation = client.submit_draft("obs_1", version=3, idempotency_key="key-3")

    request = sent(mock_api)
    assert body_of(request) == {"version": 3}
    assert request.headers["Idempotency-Key"] == "key-3"
    assert isinstance(observation, models.PublicObservation)


def test_delete_draft(client, mock_api):
    mock_api.add("DELETE", "/public/v1/observations/obs_1", httpx.Response(204))
    assert client.delete_draft("obs_1") is None
    assert sent(mock_api).method == "DELETE"


def test_path_segments_are_escaped(client, mock_api):
    path = "/public/v1/observations/obs%2F1/validation/findings/a%20b/dismiss"
    mock_api.json("DELETE", path.replace("%2F", "/").replace("%20", " "), CHECKS)
    client.undo_dismissal("obs/1", "a b")
    assert sent(mock_api).url.raw_path.decode().startswith(path)


# -- reads ------------------------------------------------------------------


def test_list_events(client, mock_api):
    mock_api.json(
        "GET",
        "/public/v1/events",
        {"items": [_event("evt_1")], "next_cursor": None},
    )
    page = client.list_events(
        status=["REVIEWED", "COMPLETE"],
        observer_names=["Ada Lovelace", "Test Observer"],
        assigned_reviewer_id=["__UNASSIGNED__"],
        limit=50,
    )

    params = sent(mock_api).url.params
    assert params["status"] == "REVIEWED,COMPLETE"
    assert params.get_list("observer_names") == ["Ada Lovelace", "Test Observer"]
    assert params.get_list("assigned_reviewer_id") == ["__UNASSIGNED__"]
    assert params["limit"] == "50"
    assert "cursor" not in params
    assert "asteroid_id" not in params
    assert isinstance(page, models.PublicEventList)
    assert page.items[0].event_id == "evt_1"


def test_get_event(client, mock_api):
    mock_api.json("GET", "/public/v1/events/evt_1", _event("evt_1"))
    event = client.get_event("evt_1")
    assert isinstance(event, models.PublicEvent)
    assert event.asteroid_name == "Doty"


def test_list_event_files(client, mock_api):
    mock_api.json(
        "GET",
        "/public/v1/events/evt_1/files",
        {"event_id": "evt_1", "files": [], "expires_in": 900},
    )
    listing = client.list_event_files("evt_1")
    assert isinstance(listing, models.PublicEventFileList)
    assert listing.expires_in == 900


def test_list_observations(client, mock_api):
    mock_api.json(
        "GET",
        "/public/v1/observations",
        {"items": [OBSERVATION], "next_cursor": "c1"},
    )
    page = client.list_observations(
        submission_status="draft",
        result_type=["POS", "UNSURE"],
        ungrouped_only=True,
    )

    params = sent(mock_api).url.params
    assert params["submission_status"] == "draft"
    assert params.get_list("result_type") == ["POS", "UNSURE"]
    assert params["ungrouped_only"] == "true"
    assert isinstance(page, models.PublicObservationList)
    assert page.next_cursor == "c1"


def test_get_observation(client, mock_api):
    mock_api.json("GET", "/public/v1/observations/obs_1", OBSERVATION)
    observation = client.get_observation("obs_1")
    assert isinstance(observation, models.PublicObservation)
    assert observation.next_actions == ["upload:lightcurve"]


def test_list_observation_files(client, mock_api):
    mock_api.json(
        "GET",
        "/public/v1/observations/obs_1/files",
        {
            "observation_id": "obs_1",
            "files": [
                {
                    "slot": "report",
                    "filename": "report.xlsx",
                    "size_bytes": 51234,
                    "url": "https://s3.example.test/get",
                    "expires_in": 900,
                }
            ],
            "zip_url": None,
            "expires_in": 900,
        },
    )
    listing = client.list_observation_files("obs_1")
    assert isinstance(listing, models.PublicFileList)
    assert listing.files[0].slot == "report"


# -- pagination -------------------------------------------------------------


def test_iter_events_follows_next_cursor(client, mock_api):
    mock_api.add(
        "GET",
        "/public/v1/events",
        httpx.Response(200, json={"items": [_event("evt_1")], "next_cursor": "c1"}),
        httpx.Response(200, json={"items": [_event("evt_2")], "next_cursor": None}),
    )
    events = list(client.iter_events(status="COMPLETE"))

    assert [event.event_id for event in events] == ["evt_1", "evt_2"]
    assert len(mock_api.requests) == 2
    assert "cursor" not in mock_api.requests[0].url.params
    assert mock_api.requests[1].url.params["cursor"] == "c1"
    assert mock_api.requests[1].url.params["status"] == "COMPLETE"


def test_iter_observations_follows_next_cursor(client, mock_api):
    second = dict(OBSERVATION, observation_id="obs_2")
    mock_api.add(
        "GET",
        "/public/v1/observations",
        httpx.Response(200, json={"items": [OBSERVATION], "next_cursor": "c1"}),
        httpx.Response(200, json={"items": [second], "next_cursor": None}),
    )
    observations = list(client.iter_observations(submission_status="draft"))

    assert [o.observation_id for o in observations] == ["obs_1", "obs_2"]
    assert mock_api.requests[1].url.params["cursor"] == "c1"


def test_iter_starts_from_a_given_cursor(client, mock_api):
    mock_api.json(
        "GET", "/public/v1/events", {"items": [_event("evt_9")], "next_cursor": None}
    )
    assert [e.event_id for e in client.iter_events(cursor="c0")] == ["evt_9"]
    assert sent(mock_api).url.params["cursor"] == "c0"


# -- wiring -----------------------------------------------------------------


def test_the_client_is_a_context_manager_over_one_transport(client, mock_api):
    assert client.http.api_root == "https://api.example.test/public/v1"


def test_every_operation_id_has_a_method():
    """The transport layer is one method per ``operation_id`` (design D15)."""
    document = json.loads(SPEC_PATH.read_text())
    operations = [
        operation["operationId"]
        for path in document["paths"].values()
        for operation in path.values()
    ]
    assert len(operations) == 16
    for operation_id in operations:
        assert operation_id.startswith("public_")
        name = operation_id[len("public_") :]
        assert callable(getattr(Client, name, None)), f"Client.{name} is missing"


def _event(event_id: str) -> dict:
    return {
        "event_id": event_id,
        "status": "COMPLETE",
        "asteroid_id": "9721",
        "asteroid_name": "Doty",
        "star_catalog": "TYC",
        "star_id": "123456",
        "date_window_utc": "2018-03-05",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
