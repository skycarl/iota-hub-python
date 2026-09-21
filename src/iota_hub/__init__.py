"""Client library for the IOTA Hub public API v1.

    from iota_hub import Client

    with Client(api_key="iotahub_...") as client:
        for observation in client.iter_observations(submission_status="draft"):
            print(observation.observation_id, observation.readiness.state)

The API contract is ``spec/openapi.json`` in this repository, and the
language-neutral client contract is ``docs/client-conventions.md``.
"""

from ._http import DEFAULT_BASE_URL, Transport, sha256_base64
from ._version import __version__
from .client import Client
from .errors import (
    AuthError,
    ConflictError,
    IotaHubError,
    NotFoundError,
    RateLimitError,
    RequestError,
    TransportError,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "AuthError",
    "Client",
    "ConflictError",
    "IotaHubError",
    "NotFoundError",
    "RateLimitError",
    "RequestError",
    "Transport",
    "TransportError",
    "__version__",
    "sha256_base64",
]
