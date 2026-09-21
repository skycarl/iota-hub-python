"""The transport: headers, base URL composition, retries, errors, uploads."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from conftest import API_KEY, BASE_URL, MockAPI

from iota_hub import (
    AuthError,
    ConflictError,
    IotaHubError,
    NotFoundError,
    RateLimitError,
    RequestError,
    Transport,
    TransportError,
    __version__,
    sha256_base64,
)
from iota_hub.models import PublicUploadTarget


def transport(mock_api: MockAPI, sleeps: list[float], **kwargs) -> Transport:
    return Transport(
        API_KEY,
        kwargs.pop("base_url", BASE_URL),
        transport=mock_api.transport,
        sleep=sleeps.append,
        **kwargs,
    )


# -- headers and URLs -------------------------------------------------------


def test_sends_the_key_user_agent_and_accept(mock_api, sleeps):
    mock_api.json("GET", "/public/v1/events", {"items": []})
    transport(mock_api, sleeps).request("GET", "/events")

    headers = mock_api.last_request.headers
    assert headers["X-API-Key"] == API_KEY
    assert headers["User-Agent"] == f"iota-hub-python/{__version__}"
    assert headers["User-Agent"].startswith("iota-hub-python/")
    assert headers["Accept"] == "application/json"


def test_user_agent_can_be_overridden(mock_api, sleeps):
    mock_api.json("GET", "/public/v1/events", {"items": []})
    transport(mock_api, sleeps, user_agent="my-tool/2.0").request("GET", "/events")
    assert mock_api.last_request.headers["User-Agent"] == "my-tool/2.0"


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://localhost:8000", "http://localhost:8000/public/v1/events"),
        ("http://localhost:8000/", "http://localhost:8000/public/v1/events"),
        (
            "https://x.execute-api.us-west-2.amazonaws.com/dev/api",
            "https://x.execute-api.us-west-2.amazonaws.com/dev/api/public/v1/events",
        ),
        (
            "https://x.execute-api.us-west-2.amazonaws.com/dev/api/",
            "https://x.execute-api.us-west-2.amazonaws.com/dev/api/public/v1/events",
        ),
    ],
)
def test_base_url_is_the_api_base_and_the_client_appends_the_prefix(
    mock_api, sleeps, base_url, expected
):
    mock_api.json("GET", "/public/v1/events", {"items": []})
    mock_api.json("GET", "/dev/api/public/v1/events", {"items": []})
    transport(mock_api, sleeps, base_url=base_url).request("GET", "/events")
    assert str(mock_api.last_request.url) == expected


def test_the_idempotency_key_is_sent_only_when_given(mock_api, sleeps):
    mock_api.json("POST", "/public/v1/observations/drafts", {}, status=201)
    http = transport(mock_api, sleeps)

    http.request("POST", "/observations/drafts")
    assert "Idempotency-Key" not in mock_api.last_request.headers

    http.request("POST", "/observations/drafts", idempotency_key="key-1")
    assert mock_api.last_request.headers["Idempotency-Key"] == "key-1"


# -- retries ----------------------------------------------------------------


def test_429_is_retried_honouring_retry_after_with_the_same_idempotency_key(
    mock_api, sleeps
):
    mock_api.add(
        "POST",
        "/public/v1/observations/drafts",
        httpx.Response(
            429,
            json={"code": "rate_limited", "message": "slow down"},
            headers={"Retry-After": "7"},
        ),
        httpx.Response(201, json={"observation": None}),
    )
    transport(mock_api, sleeps).request(
        "POST", "/observations/drafts", idempotency_key="key-1"
    )

    assert sleeps == [7.0]
    assert len(mock_api.requests) == 2
    assert {r.headers["Idempotency-Key"] for r in mock_api.requests} == {"key-1"}


def test_503_is_retried_then_succeeds(mock_api, sleeps):
    mock_api.add(
        "GET",
        "/public/v1/events",
        httpx.Response(503, text="<html>bad gateway</html>"),
        httpx.Response(200, json={"items": []}),
    )
    response = transport(mock_api, sleeps).request("GET", "/events")

    assert response.status_code == 200
    assert sleeps == [0.5]


def test_a_transport_failure_is_retried_then_raises_transport_error(mock_api, sleeps):
    mock_api.add(
        "GET", "/public/v1/events", httpx.ConnectError("name resolution failed")
    )
    with pytest.raises(TransportError) as raised:
        transport(mock_api, sleeps, max_retries=2).request("GET", "/events")

    assert len(mock_api.requests) == 3
    assert sleeps == [0.5, 1.0]
    assert isinstance(raised.value.cause, httpx.ConnectError)
    assert raised.value.status is None


def test_409_is_never_retried(mock_api, sleeps):
    mock_api.json(
        "POST",
        "/public/v1/observations/obs_1/submit",
        {"code": "open_findings", "message": "nope"},
        status=409,
    )
    with pytest.raises(ConflictError):
        transport(mock_api, sleeps).request("POST", "/observations/obs_1/submit")

    assert len(mock_api.requests) == 1
    assert sleeps == []


def test_max_retries_then_raises_rate_limit_error_with_retry_after(mock_api, sleeps):
    mock_api.json(
        "GET",
        "/public/v1/events",
        {"code": "rate_limited", "message": "too many", "hint": "wait"},
        status=429,
        headers={"Retry-After": "3"},
    )
    with pytest.raises(RateLimitError) as raised:
        transport(mock_api, sleeps, max_retries=2).request("GET", "/events")

    assert len(mock_api.requests) == 3
    assert sleeps == [3.0, 3.0]
    assert raised.value.retry_after == 3.0
    assert raised.value.status == 429


def test_backoff_is_exponential_and_capped(mock_api, sleeps):
    body = {"code": "internal_error", "message": "x"}
    mock_api.json("GET", "/public/v1/events", body, status=500)
    with pytest.raises(IotaHubError):
        transport(mock_api, sleeps, max_retries=7).request("GET", "/events")

    assert sleeps == [0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 20.0]


def test_retry_after_accepts_an_http_date(mock_api, sleeps):
    mock_api.add(
        "GET",
        "/public/v1/events",
        httpx.Response(
            429,
            json={"code": "rate_limited", "message": "slow down"},
            headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"},
        ),
        httpx.Response(200, json={"items": []}),
    )
    transport(mock_api, sleeps).request("GET", "/events")
    assert sleeps[0] > 0


# -- error mapping ----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (401, "missing_api_key", AuthError),
        (401, "expired_api_key", AuthError),
        (403, "insufficient_scope", AuthError),
        (403, "forbidden", AuthError),
        (404, "not_found", NotFoundError),
        (409, "stale_version", ConflictError),
        (409, "upload_missing", ConflictError),
        (422, "missing_required", RequestError),
        (422, "file_too_large", RequestError),
        (400, "validation_error", RequestError),
        (429, "rate_limited", RateLimitError),
        (500, "internal_error", IotaHubError),
    ],
)
def test_problem_details_map_to_the_right_exception(
    mock_api, sleeps, status, code, expected
):
    mock_api.json(
        "GET",
        "/public/v1/events",
        {"code": code, "message": "m", "hint": "h", "details": {"slot": "log"}},
        status=status,
    )
    with pytest.raises(expected) as raised:
        transport(mock_api, sleeps, max_retries=0).request("GET", "/events")

    error = raised.value
    assert type(error) is expected
    assert (error.code, error.status, error.hint) == (code, status, "h")
    assert error.details == {"slot": "log"}


def test_str_reads_like_the_api_says_it(mock_api, sleeps):
    mock_api.json(
        "POST",
        "/public/v1/observations/obs_1/submit",
        {
            "code": "open_findings",
            "message": "Draft has findings that are neither fixed nor dismissed",
            "hint": "Fix the listed findings, or dismiss each one with a note.",
        },
        status=409,
    )
    with pytest.raises(ConflictError) as raised:
        transport(mock_api, sleeps).request("POST", "/observations/obs_1/submit")

    assert str(raised.value) == (
        "open_findings: Draft has findings that are neither fixed nor dismissed "
        "(hint: Fix the listed findings, or dismiss each one with a note.)"
    )


def test_a_non_json_502_becomes_internal_error(mock_api, sleeps):
    mock_api.add(
        "GET", "/public/v1/events", httpx.Response(502, text="<html>Bad Gateway</html>")
    )
    with pytest.raises(IotaHubError) as raised:
        transport(mock_api, sleeps, max_retries=0).request("GET", "/events")

    assert raised.value.code == "internal_error"
    assert raised.value.status == 502
    assert raised.value.details == {}


def test_a_gateway_429_without_a_body_is_still_a_rate_limit_error(mock_api, sleeps):
    mock_api.add(
        "GET",
        "/public/v1/events",
        httpx.Response(429, text="Too Many Requests", headers={"Retry-After": "2"}),
    )
    with pytest.raises(RateLimitError) as raised:
        transport(mock_api, sleeps, max_retries=0).request("GET", "/events")

    assert raised.value.code == "rate_limited"
    assert raised.value.retry_after == 2.0


def test_a_non_json_418_falls_back_to_http_error(mock_api, sleeps):
    mock_api.add("GET", "/public/v1/events", httpx.Response(418, text="teapot"))
    with pytest.raises(IotaHubError) as raised:
        transport(mock_api, sleeps, max_retries=0).request("GET", "/events")

    assert raised.value.code == "http_error"


# -- S3 uploads -------------------------------------------------------------


def _target(**fields: str) -> PublicUploadTarget:
    return PublicUploadTarget(
        slot="lightcurve",
        filename="lightcurve.csv",
        upload_url="https://s3.example.test/bucket",
        fields=fields or {"key": "staging/obs_1/lightcurve/lightcurve.csv"},
        file_key="staging/obs_1/lightcurve/lightcurve.csv",
    )


def test_upload_posts_the_fields_in_order_with_the_file_last(mock_api, sleeps):
    mock_api.add("POST", "/bucket", httpx.Response(204))
    target = _target(
        key="staging/obs_1/lightcurve/lightcurve.csv",
        policy="eyJleHBpcmF0aW9uIjoi",
        **{"x-amz-signature": "1a2b3c"},
    )
    transport(mock_api, sleeps).upload_to_s3(target, b"time,flux\n")

    body = mock_api.last_request.content.decode()
    names = [
        line.split('name="', 1)[1].split('"', 1)[0]
        for line in body.splitlines()
        if 'name="' in line
    ]
    assert names == ["key", "policy", "x-amz-signature", "file"]
    assert "time,flux" in body


def test_upload_never_sends_the_api_key(mock_api, sleeps):
    mock_api.add("POST", "/bucket", httpx.Response(204))
    transport(mock_api, sleeps).upload_to_s3(_target(), b"time,flux\n")

    headers = mock_api.last_request.headers
    assert "X-API-Key" not in headers
    assert API_KEY not in str(headers)
    assert headers["User-Agent"].startswith("iota-hub-python/")


def test_upload_accepts_a_path(mock_api, sleeps, tmp_path: Path):
    path = tmp_path / "lightcurve.csv"
    path.write_bytes(b"time,flux\n1,2\n")
    mock_api.add("POST", "/bucket", httpx.Response(201))
    transport(mock_api, sleeps).upload_to_s3(_target(), path)

    assert "1,2" in mock_api.last_request.content.decode()


def test_a_rejected_upload_raises_upload_failed(mock_api, sleeps):
    mock_api.add("POST", "/bucket", httpx.Response(403, text="<Error>...</Error>"))
    with pytest.raises(IotaHubError) as raised:
        transport(mock_api, sleeps).upload_to_s3(_target(), b"x")

    assert raised.value.code == "upload_failed"
    assert raised.value.status == 403
    assert raised.value.details["slot"] == "lightcurve"


def test_an_unreachable_bucket_raises_transport_error(mock_api, sleeps):
    mock_api.add("POST", "/bucket", httpx.ConnectError("no route to host"))
    with pytest.raises(TransportError):
        transport(mock_api, sleeps).upload_to_s3(_target(), b"x")


# -- digests ----------------------------------------------------------------


def test_sha256_base64_matches_openssl(tmp_path: Path):
    path = tmp_path / "known.txt"
    path.write_bytes(b"iota-hub\n")
    # printf 'iota-hub\n' | openssl dgst -binary -sha256 | base64
    assert sha256_base64(path) == "LcAnZMo1c67rilnVHRJv8Pc72nJc2MhPuC+YzpBEzGU="


def test_sha256_base64_streams_a_large_file(tmp_path: Path):
    path = tmp_path / "big.csv"
    path.write_bytes(b"x" * (3 * 1024 * 1024 + 7))
    assert len(sha256_base64(path)) == 44
