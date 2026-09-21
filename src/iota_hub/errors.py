"""Exceptions for the IOTA Hub public API.

Every ``4xx``/``5xx`` on ``/public/v1`` is a problem-details body
``{code, message, hint, details}``. One exception type carries all of it, and
the subclasses group the codes by category so a caller can catch broadly. The
contract is ``code``: branch on it, never on ``message``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

#: Authentication and authorization reason codes (specs/public-api.md § 8).
AUTH_CODES = frozenset(
    {
        "missing_api_key",
        "malformed_api_key",
        "unknown_api_key",
        "invalid_api_key",
        "expired_api_key",
        "inactive_user",
        "insufficient_scope",
        "forbidden",
    }
)


class IotaHubError(Exception):
    """A problem-details response, or a failure to get one."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details: dict[str, Any] = details or {}
        self.status = status
        self.retry_after = retry_after

    def __str__(self) -> str:
        text = f"{self.code}: {self.message}"
        if self.hint:
            text = f"{text} (hint: {self.hint})"
        return text

    @classmethod
    def from_response(cls, response: httpx.Response) -> IotaHubError:
        """Build the right subclass from an error response."""
        status = response.status_code
        body = _problem_details(response)
        if body is None:
            code = _fallback_code(status)
            message = f"HTTP {status} from {response.request.url}"
            hint = None
            details: dict[str, Any] = {}
        else:
            code = body["code"]
            message = body.get("message") or code
            hint = body.get("hint")
            details = body.get("details") or {}
        error_class = _class_for(code, status)
        return error_class(
            code,
            message,
            hint=hint,
            details=details,
            status=status,
            retry_after=parse_retry_after(response.headers.get("Retry-After")),
        )


class AuthError(IotaHubError):
    """The key is missing, bad, expired, or lacks the scope or permission."""


class RateLimitError(IotaHubError):
    """A ``429``. ``retry_after`` is the wait in seconds when the API gave one."""


class NotFoundError(IotaHubError):
    """A ``404``. A resource that is not yours reports this too, never ``403``."""


class ConflictError(IotaHubError):
    """A ``409`` — the resource is not in a state that allows the call."""


class RequestError(IotaHubError):
    """A ``400``/``422`` — the request itself was rejected."""


class TransportError(IotaHubError):
    """The request never produced a response (DNS, connect, read, TLS)."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__("transport_error", message)
        self.cause = cause


def parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` as seconds, from either form, or ``None``."""
    if not value:
        return None
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _problem_details(response: httpx.Response) -> dict[str, Any] | None:
    """The body as problem details, or ``None`` when it is not one.

    A gateway-level ``429`` or ``5xx`` answers HTML, and a truncated response
    answers nothing at all; neither is an error worth raising over the error.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict) and isinstance(body.get("code"), str):
        return body
    return None


def _fallback_code(status: int) -> str:
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "internal_error"
    return "http_error"


def _class_for(code: str, status: int) -> type[IotaHubError]:
    if status == 429 or code == "rate_limited":
        return RateLimitError
    if code in AUTH_CODES or status in (401, 403):
        return AuthError
    if code == "not_found" or status == 404:
        return NotFoundError
    if status == 409:
        return ConflictError
    if status in (400, 422):
        return RequestError
    return IotaHubError
