"""Configuration: flag > environment > config file.

The stored file keeps a key **and** its base URL together, per profile, so a
dev key is never sent to prod or the reverse (design § 8.2). A key is never a
command-line argument, never logged, and never part of an error message — the
most a caller ever sees is :func:`key_prefix`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_dir

from ._http import DEFAULT_BASE_URL
from .errors import AuthError, IotaHubError

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 only
    import tomli as tomllib  # type: ignore[no-redef]

#: Environment variables, highest precedence after an explicit argument.
ENV_API_KEY = "IOTA_HUB_API_KEY"
ENV_BASE_URL = "IOTA_HUB_BASE_URL"
ENV_PROFILE = "IOTA_HUB_PROFILE"

CONFIG_FILENAME = "config.toml"


@dataclass
class Settings:
    """A resolved target: which key, which deployment, and where they came from."""

    api_key: str
    base_url: str
    profile_name: str | None
    source_of_key: str
    is_default_target: bool


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def resolve(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    profile: str | None = None,
    env: Mapping[str, str] = os.environ,
    config_path: str | Path | None = None,
) -> Settings:
    """Settle the key and the target, highest precedence first.

    Key: argument, then ``IOTA_HUB_API_KEY``, then the profile's stored key.
    Target: argument, then ``IOTA_HUB_BASE_URL``, then the profile's base URL,
    then the production default. Passing ``--base-url`` while using a profile
    is allowed and the flag wins — an explicit instruction beats a stored one.
    """
    config = load_config(config_path)
    profiles = config.get("profiles") or {}
    requested = profile or env.get(ENV_PROFILE) or None
    name = requested or config.get("default_profile") or None

    stored: dict[str, str] = {}
    if name is not None:
        found = profiles.get(name)
        if found is None:
            if requested is not None:
                raise IotaHubError(
                    "unknown_profile",
                    f"No profile named {name!r} in {config_file_path(config_path)}.",
                    hint=(
                        "Create it with `iota-hub auth login --profile "
                        f"{name} --base-url <url>`."
                    ),
                    details={"profile": name, "known": sorted(profiles)},
                )
            name = None
        else:
            stored = found

    if api_key:
        key, source = api_key, "flag"
    elif env.get(ENV_API_KEY):
        key, source = env[ENV_API_KEY], "env"
    elif stored.get("api_key"):
        key, source = stored["api_key"], "profile"
    else:
        raise AuthError(
            "missing_api_key",
            "No API key: nothing to authenticate with.",
            hint="Set IOTA_HUB_API_KEY or run `iota-hub auth login`.",
        )

    target = (
        base_url or env.get(ENV_BASE_URL) or stored.get("base_url") or DEFAULT_BASE_URL
    ).rstrip("/")

    return Settings(
        api_key=key,
        base_url=target,
        profile_name=name,
        source_of_key=source,
        is_default_target=target == DEFAULT_BASE_URL,
    )


def key_prefix(key: str) -> str:
    """The identifying head of a key, safe to print: ``iotahub_<key_id>_...``.

    The secret tail is never returned, by this or by anything else — the whole
    point of the helper is that ``auth status`` has something to show.
    """
    parts = key.split("_")
    if len(parts) >= 3 and parts[0] == "iotahub" and parts[1]:
        return f"iotahub_{parts[1]}_..."
    return "<unrecognized key format>"


# --------------------------------------------------------------------------
# The config file
# --------------------------------------------------------------------------


def config_file_path(config_path: str | Path | None = None) -> Path:
    """Where the config lives: the platform config dir, or an explicit path."""
    if config_path is not None:
        return Path(config_path)
    # ``appauthor=False`` keeps Windows at %APPDATA%\iota-hub rather than
    # %APPDATA%\iota-hub\iota-hub, which is what the docs promise.
    return Path(user_config_dir("iota-hub", appauthor=False)) / CONFIG_FILENAME


def load_config(config_path: str | Path | None = None) -> dict:
    """The parsed config file, or ``{}`` when there is none."""
    path = config_file_path(config_path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise IotaHubError(
            "invalid_config",
            f"{path} is not valid TOML: {exc}",
            hint="Fix the file, or delete it and run `iota-hub auth login`.",
            details={"path": str(path)},
        ) from exc


def save_profile(
    name: str,
    *,
    api_key: str,
    base_url: str,
    config_path: str | Path | None = None,
) -> Path:
    """Store one profile's key and base URL, and return the file's path.

    The first profile written becomes ``default_profile``.
    """
    _check_writable("profile name", name, forbidden='"\\')
    _check_writable("base_url", base_url)
    _check_writable("API key", api_key)
    config = load_config(config_path)
    profiles = dict(config.get("profiles") or {})
    profiles[name] = {"base_url": base_url.rstrip("/"), "api_key": api_key}
    config["profiles"] = profiles
    config.setdefault("default_profile", name)
    return _write_config(config, config_path)


def delete_profile(name: str, *, config_path: str | Path | None = None) -> bool:
    """Forget one profile. ``False`` if there was nothing to forget."""
    config = load_config(config_path)
    profiles = dict(config.get("profiles") or {})
    if name not in profiles:
        return False
    del profiles[name]
    config["profiles"] = profiles
    if config.get("default_profile") == name:
        # Point at whatever is left rather than at a profile that is gone.
        remaining = sorted(profiles)
        if remaining:
            config["default_profile"] = remaining[0]
        else:
            config.pop("default_profile", None)
    _write_config(config, config_path)
    return True


def _write_config(config: dict, config_path: str | Path | None) -> Path:
    path = config_file_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _to_toml(config)
    # 0600 at creation, so the key is never briefly world-readable. On Windows
    # the mode is ignored and the user profile directory's ACL is the
    # protection — which is what the docs promise there.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    if os.name != "nt":
        # O_CREAT's mode only applies to a file this call created; an existing
        # one keeps whatever it had until we say otherwise.
        os.chmod(path, 0o600)
    return path


def _to_toml(config: dict) -> str:
    """The two-key shape this file has, written by hand.

    Small enough that a TOML *writer* dependency would cost more than it saves,
    and everything written here is a string this module produced.
    """
    lines = []
    default = config.get("default_profile")
    if default:
        lines.append(f"default_profile = {_quote(default)}")
        lines.append("")
    for name, profile in sorted((config.get("profiles") or {}).items()):
        lines.append(f"[profiles.{_quote(name)}]")
        for key in ("base_url", "api_key"):
            value = profile.get(key)
            if value:
                lines.append(f"{key} = {_quote(value)}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _check_writable(what: str, value: str, *, forbidden: str = "") -> None:
    """Refuse a value that would make the config file unreadable.

    A control character cannot go in a TOML string at all, and a profile name
    is also a table header (``[profiles."dev"]``), so it additionally cannot
    carry a quote or a backslash. Everything else is escaped on the way out
    (:func:`_quote`), so a backslash in a base URL round-trips.
    """
    bad = [
        character
        for character in value
        if character < " " or character == "\x7f" or character in forbidden
    ]
    if bad:
        raise IotaHubError(
            "invalid_config",
            f"The {what} contains a character that cannot be stored: {bad[0]!r}.",
            hint="Use a plain name and a plain URL.",
            details={"field": what},
        )


def _quote(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
