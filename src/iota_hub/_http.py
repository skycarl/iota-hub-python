"""The HTTP layer: one retrying request method, and the S3 upload.

Everything above this module works in models; everything below is ``httpx``.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from pathlib import Path
from time import sleep as _default_sleep
from typing import Any
from urllib.parse import urlsplit

import httpx

from ._version import __version__
from .errors import IotaHubError, TransportError, parse_retry_after
from .models import PublicUploadTarget

#: The production API base. The client appends :data:`API_PREFIX` (design D16).
DEFAULT_BASE_URL = "https://api.iota-hub.com"

#: The versioned public surface, appended to whatever base URL is configured.
API_PREFIX = "/public/v1"

#: Backoff is ``BACKOFF_BASE * 2 ** attempt`` seconds, capped.
BACKOFF_BASE = 0.5
BACKOFF_CAP = 20.0

_CHUNK = 1024 * 1024

#: Hosts a key may be sent to over plain ``http``: the local machine only.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class Transport:
    """Sends requests to one IOTA Hub deployment with one API key.

    ``base_url`` is the **API base**, not the versioned surface: the transport
    appends ``/public/v1`` itself, so ``http://localhost:8000`` and
    ``https://x.execute-api.us-west-2.amazonaws.com/dev/api`` both work.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 30.0,
        max_retries: int = 4,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
        sleep: Callable[[float], None] = _default_sleep,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        check_base_url(self.base_url)
        self.api_root = f"{self.base_url}{API_PREFIX}"
        self.max_retries = max_retries
        self.user_agent = user_agent or f"iota-hub-python/{__version__}"
        #: Injectable so tests never wait (conventions § 6).
        self.sleep = sleep
        #: The shared httpx client. It carries **no** API key of its own:
        #: :meth:`request` adds the header per call, so a presigned S3 URL
        #: (upload or download) can use this same client safely.
        self.client = httpx.Client(timeout=timeout, transport=transport)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Transport:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- API requests ------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        """Call the API and return the response, or raise an :class:`IotaHubError`.

        Retries ``429``, ``5xx`` and transport failures with exponential backoff,
        honouring ``Retry-After`` when the response carries one. Any other
        ``4xx`` is raised immediately — retrying a rejected request only wastes
        the caller's rate-limit budget.
        """
        url = f"{self.api_root}{path}"
        headers = {
            "X-API-Key": self.api_key,
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        query = _clean_params(params)

        attempt = 0
        while True:
            try:
                response = self.client.request(
                    method, url, json=json, params=query, headers=headers
                )
            except httpx.HTTPError as exc:
                if attempt >= self.max_retries:
                    raise TransportError(
                        f"{method} {url} failed: {exc}", cause=exc
                    ) from exc
                self.sleep(_backoff(attempt))
                attempt += 1
                continue

            if response.status_code < 400:
                return response
            if not _retryable(response.status_code) or attempt >= self.max_retries:
                raise IotaHubError.from_response(response)

            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            self.sleep(retry_after if retry_after is not None else _backoff(attempt))
            attempt += 1

    # -- S3 uploads --------------------------------------------------------

    def upload_to_s3(
        self, target: PublicUploadTarget, path_or_bytes: str | Path | bytes
    ) -> None:
        """POST one file to its presigned target.

        The API key is deliberately **not** sent: the presigned form is the
        credential, and S3 has no business seeing the key. Every entry of
        ``fields`` goes into the form exactly as returned, in order, and the
        file part comes last.
        """
        if isinstance(path_or_bytes, bytes):
            content: Any = path_or_bytes
        else:
            content = Path(path_or_bytes).read_bytes()

        try:
            response = self.client.post(
                target.upload_url,
                data=dict(target.fields),
                files={"file": (target.filename, content)},
                headers={"User-Agent": self.user_agent},
            )
        except httpx.HTTPError as exc:
            raise TransportError(
                f"upload of {target.filename} to S3 failed: {exc}", cause=exc
            ) from exc

        if response.is_success:
            return
        raise IotaHubError(
            "upload_failed",
            f"S3 rejected the upload of {target.filename} "
            f"with HTTP {response.status_code}",
            hint="Re-initialize the upload and post the returned fields unchanged.",
            details={"slot": target.slot, "filename": target.filename},
            status=response.status_code,
        )


def check_base_url(base_url: str) -> None:
    """Refuse a base URL the key would travel to in cleartext.

    The key is a header on every API call, so anything but ``https`` would
    hand it to the network. Plain ``http`` is allowed only for the local
    machine, where a development server runs (conventions § 4).
    """
    parts = urlsplit(base_url)
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS:
        return
    raise IotaHubError(
        "invalid_config",
        f"Refusing to send the API key to {base_url!r}: the base URL must be "
        "https (plain http is allowed only for localhost).",
        hint="Use the https:// address of the API.",
        details={"field": "base_url", "base_url": base_url},
    )


def sha256_base64(path: str | Path) -> str:
    """The file's SHA-256 as base64, which is what the API's ``sha256`` wants.

    The same value as ``openssl dgst -binary -sha256 <file> | base64``.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def _backoff(attempt: int) -> float:
    return min(BACKOFF_BASE * 2**attempt, BACKOFF_CAP)


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop ``None`` values and empty lists; httpx encodes the rest."""
    if not params:
        return None
    cleaned = {
        key: value for key, value in params.items() if value is not None and value != []
    }
    return cleaned or None
