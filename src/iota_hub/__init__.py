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
from .config import (
    Settings,
    config_file_path,
    delete_profile,
    key_prefix,
    load_config,
    resolve,
    save_profile,
)
from .errors import (
    AuthError,
    ConflictError,
    IotaHubError,
    NotFoundError,
    RateLimitError,
    RequestError,
    TransportError,
)
from .files import SLOTS, FolderMapping, MappingError, map_folder
from .workflow import SubmitResult, next_actions_commands

__all__ = [
    "DEFAULT_BASE_URL",
    "SLOTS",
    "AuthError",
    "Client",
    "ConflictError",
    "FolderMapping",
    "IotaHubError",
    "MappingError",
    "NotFoundError",
    "RateLimitError",
    "RequestError",
    "Settings",
    "SubmitResult",
    "Transport",
    "TransportError",
    "__version__",
    "config_file_path",
    "delete_profile",
    "key_prefix",
    "load_config",
    "map_folder",
    "next_actions_commands",
    "resolve",
    "save_profile",
    "sha256_base64",
]
