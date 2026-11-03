"""Configuration: flag > environment > config file.

The stored file keeps a key **and** its base URL together, per profile, so a
dev key is never sent to prod or the reverse (design § 8.2). A key is never a
command-line argument, never logged, and never part of an error message — the
most a caller ever sees is :func:`key_prefix`.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_dir

from ._http import DEFAULT_BASE_URL
from .errors import AuthError, IotaHubError

_TEMP_NAME_ATTEMPTS = 100

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


def select_profile(
    config: Mapping,
    profile: str | None = None,
    *,
    env: Mapping[str, str] = os.environ,
) -> tuple[str, bool]:
    """Which profile a command means, and whether it was asked for by name.

    ``profile``, then ``IOTA_HUB_PROFILE``, then ``default_profile``, then
    ``default``. Every command selects this way, ``auth login`` and ``auth
    logout`` included. A ``default_profile`` that no longer exists is ignored,
    so deleting a profile never wedges the CLI. The name may not exist: what
    that means is the caller's to decide.
    """
    requested = profile or env.get(ENV_PROFILE)
    if requested:
        return requested, True
    stored = config.get("default_profile")
    if stored and stored in (config.get("profiles") or {}):
        return stored, False
    return "default", False


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
    ``IOTA_HUB_BASE_URL`` is not explicit enough to do the same: when it
    disagrees with the base URL stored beside the profile's key, that is
    ``invalid_config`` rather than the key going somewhere it was not stored for.
    """
    config = load_config(config_path)
    profiles = config.get("profiles") or {}
    name, requested = select_profile(config, profile, env=env)

    stored: dict[str, str] = profiles.get(name) or {}
    if name not in profiles:
        if requested:
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

    env_target = (env.get(ENV_BASE_URL) or "").rstrip("/")
    stored_target = (stored.get("base_url") or "").rstrip("/")
    if (
        not base_url
        and source == "profile"
        and env_target
        and stored_target
        and env_target != stored_target
    ):
        # The stored key belongs to the stored target. An ambient environment
        # variable must not quietly redirect it elsewhere -- a leftover dev
        # IOTA_HUB_BASE_URL would otherwise send the prod key to dev.
        raise IotaHubError(
            "invalid_config",
            f"{ENV_BASE_URL} is {env_target}, but the key comes from profile "
            f"{name!r}, which is stored for {stored_target}.",
            hint=(
                f"Unset {ENV_BASE_URL}, set {ENV_API_KEY} as well, or pass "
                "--base-url to choose the target explicitly."
            ),
            details={"field": "base_url", "profile": name},
        )

    target = (base_url or env_target or stored_target or DEFAULT_BASE_URL).rstrip("/")

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
    # ``appauthor=False`` keeps Windows at %LOCALAPPDATA%\iota-hub rather than
    # %LOCALAPPDATA%\iota-hub\iota-hub, which is what the docs promise.
    return Path(user_config_dir("iota-hub", appauthor=False)) / CONFIG_FILENAME


def load_config(config_path: str | Path | None = None) -> dict:
    """The parsed config file, or ``{}`` when there is none."""
    path = config_file_path(config_path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise IotaHubError(
            "invalid_config",
            f"Cannot read {path}: {getattr(exc, 'strerror', None) or exc}.",
            hint="Fix the file, or delete it and run `iota-hub auth login`.",
            details={"path": str(path)},
        ) from exc
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

    The first profile written becomes ``default_profile``, as does any profile
    written while a bare command would select nothing.
    """
    _check_writable("profile name", name, forbidden='"\\')
    _check_writable("base_url", base_url)
    _check_writable("API key", api_key)
    config = load_config(config_path)
    profiles = dict(config.get("profiles") or {})
    if select_profile(config, env={})[0] not in profiles:
        config["default_profile"] = name
    profiles[name] = {"base_url": base_url.rstrip("/"), "api_key": api_key}
    config["profiles"] = profiles
    return _write_config(config, config_path)


def delete_profile(name: str, *, config_path: str | Path | None = None) -> bool:
    """Forget one profile. ``False`` if there was nothing to forget.

    If it was ``default_profile``, no profile takes its place: bare commands
    fall back to a profile named ``default``, as :func:`select_profile` says.
    """
    config = load_config(config_path)
    profiles = dict(config.get("profiles") or {})
    if name not in profiles:
        return False
    del profiles[name]
    config["profiles"] = profiles
    if config.get("default_profile") == name:
        # Cleared, never repointed: choosing another profile here would quietly
        # aim every later bare command at a different deployment.
        del config["default_profile"]
    _write_config(config, config_path)
    return True


def _create_temp_file(directory: str) -> tuple[int, str]:
    """Create a private temp file in ``directory``, failing fast if refused.

    Not ``tempfile.mkstemp``: on Windows it takes a permission error for a name
    clash and retries about two billion times, so a folder whose ACL denies
    writes would hang instead of raising.
    """
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOINHERIT", 0)
    )
    for _ in range(_TEMP_NAME_ATTEMPTS):
        temp = os.path.join(directory, f".config-{secrets.token_hex(8)}.tmp")
        try:
            return os.open(temp, flags, 0o600), temp
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, "No free temporary file name", directory)


def _write_config(config: dict, config_path: str | Path | None) -> Path:
    path = config_file_path(config_path)
    text = _to_toml(config)
    temp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A symlinked config (a dotfiles manager) stays a symlink: the replace
        # lands on the file it points at.
        target = os.path.realpath(path)
        # The temp file is 0600 at creation, so the key is never briefly
        # world-readable, and the replace leaves the file 0600 whatever it was
        # before. On Windows the mode is ignored and the user profile
        # directory's ACL is the protection — which is what the docs promise.
        descriptor, temp = _create_temp_file(os.path.dirname(target))
        with open(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # All or nothing: a failure above leaves the old file untouched.
        os.replace(temp, target)
    except OSError as exc:
        raise IotaHubError(
            "not_writable",
            f"Cannot write the config file {path}: {exc.strerror or exc}.",
            hint=(
                f"Fix the permissions on {path.parent}, or set "
                f"{ENV_API_KEY} instead of storing a key."
            ),
            details={"path": str(path)},
        ) from exc
    finally:
        if temp is not None:
            with contextlib.suppress(OSError):
                os.unlink(temp)
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
