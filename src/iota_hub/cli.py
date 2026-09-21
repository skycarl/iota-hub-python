"""The ``iota-hub`` command: a thin layer over the library.

Every decision this module makes is about *presentation* -- which stream a line
goes to, how an error becomes an exit code, what a ``--json`` document looks
like. The work itself belongs to :mod:`iota_hub.workflow` and
:mod:`iota_hub.client` (design section 8.1: "the ``iota-hub`` command as a thin
layer on it"), so an embedder gets the same behavior without the CLI.

The conventions are ``docs/client-conventions.md`` sections 12-13: data on
stdout, progress and ``Target:`` on stderr, one JSON document on stdout under
``--json`` and a problem-details object on stderr when it fails, documented
exit codes, ASCII only, and never a prompt when stdin is not a TTY.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import uuid
from dataclasses import dataclass
from enum import Enum
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

try:  # typer 0.27+ vendors click; older typer depends on the package.
    from typer._click.exceptions import UsageError
except ImportError:  # pragma: no cover - typer < 0.27 only
    from click.exceptions import UsageError  # type: ignore[no-redef]

from . import config
from ._http import DEFAULT_BASE_URL
from ._version import __version__
from .client import Client
from .config import ENV_BASE_URL, Settings
from .errors import AuthError, ConflictError, IotaHubError, RateLimitError
from .files import map_folder
from .models import PublicChecks, PublicEvent, PublicObservation
from .workflow import POLL_TIMEOUT, SubmitResult, next_actions_commands

#: Printed by ``guide`` when the bundled guide is not in the install (D18).
GUIDE_FALLBACK = (
    "The workflow guide is not bundled with this build. "
    "Read it at https://iota-hub.com/developers"
)

#: ``--dry-run`` touches no endpoint, but the resolver still wants a key.
#: Only the resolved base URL is read back from the settings it returns.
_UNUSED_KEY = "(no key needed)"

#: Codes that mean the *invocation* was wrong (conventions section 12): exit 2.
USAGE_CODES = frozenset(
    {
        "ambiguous_files",
        "missing_files",
        "invalid_extension",
        "not_a_directory",
        "unknown_profile",
        "confirmation_required",
    }
)

#: Codes that mean "not ready", with nothing wrong with the call: exit 4.
NOT_READY_CODES = frozenset(
    {
        "timeout",
        "open_findings",
        "missing_required",
        "checks_stale",
        "event_files_conflict",
    }
)


class SlotName(str, Enum):
    """The file slots the public API accepts."""

    report = "report"
    lightcurve = "lightcurve"
    log = "log"
    vizier = "vizier"


class ResourceKind(str, Enum):
    """What ``files download`` can download."""

    observation = "observation"
    event = "event"


# --------------------------------------------------------------------------
# Global options
# --------------------------------------------------------------------------


@dataclass
class Options:
    """The root app's options, settled by the callback before any command."""

    base_url: str | None = None
    profile: str | None = None
    json: bool = False


_OPTIONS = Options()


# --------------------------------------------------------------------------
# Errors: one mapping, one handler
# --------------------------------------------------------------------------


def exit_code_for(error: IotaHubError) -> int:
    """The documented exit code for one error (conventions section 12).

    The order matters: a code that names the situation wins over the
    exception's category, so a ``422 missing_required`` is "not ready" (``4``)
    rather than a plain error, while ``missing_api_key`` stays auth (``3``).
    """
    if error.code in USAGE_CODES:
        return 2
    if error.code in NOT_READY_CODES:
        return 4
    if isinstance(error, AuthError):
        return 3
    if isinstance(error, RateLimitError):
        return 5
    return 1


def handle_errors(command):
    """The one wrapper around every command: shared options in, errors out.

    It turns any :class:`IotaHubError` into the documented output and exit
    code, and it appends ``--base-url``, ``--profile`` and ``--json`` to the
    command's signature, so the three are accepted *after* the subcommand
    (`iota-hub submit --json`) as well as before it, without any command
    having to declare them.
    """

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        _merge_options(
            base_url=kwargs.pop("base_url", None),
            profile=kwargs.pop("profile", None),
            as_json=kwargs.pop("json", False),
        )
        try:
            return command(*args, **kwargs)
        except IotaHubError as error:
            _fail(error)

    wrapper.__name__ = command.__name__
    wrapper.__doc__ = command.__doc__
    wrapper.__wrapped__ = command
    signature = inspect.signature(command, eval_str=True)
    wrapper.__signature__ = signature.replace(
        parameters=[*signature.parameters.values(), *_shared_options()]
    )
    return wrapper


def _merge_options(*, base_url: str | None, profile: str | None, as_json: bool) -> None:
    """Fold one command's copy of the shared options into the root's.

    A value given on the leaf wins; one that was not given leaves the root's
    alone, so ``iota-hub --json submit DIR`` and ``iota-hub submit DIR --json``
    are the same run.
    """
    _OPTIONS.base_url = base_url or _OPTIONS.base_url
    _OPTIONS.profile = profile or _OPTIONS.profile
    _OPTIONS.json = as_json or _OPTIONS.json


def _fail(error: IotaHubError) -> NoReturn:
    code = exit_code_for(error)
    if _OPTIONS.json:
        _err(
            json.dumps(
                {
                    "code": error.code,
                    "message": error.message,
                    "hint": error.hint,
                    "details": error.details,
                    "status": error.status,
                    "retry_after": error.retry_after,
                    "exit_code": code,
                },
                indent=2,
            )
        )
    else:
        _err(f"error: {error.code}: {error.message}")
        if error.hint:
            _err(f"hint: {error.hint}")
        if error.retry_after is not None:
            _err(f"retry after {error.retry_after:g} s")
    raise typer.Exit(code)


# --------------------------------------------------------------------------
# Streams
# --------------------------------------------------------------------------


def _out(line: str = "") -> None:
    """One line of data, on stdout."""
    typer.echo(line)


def _err(line: str) -> None:
    """One line of progress, diagnostics or targeting, on stderr."""
    typer.echo(line, err=True)


def _emit(payload: Any) -> None:
    """The one JSON document a ``--json`` run prints."""
    _out(json.dumps(payload, indent=2))


def _progress(message: str) -> None:
    _err(message)


def _table(headers: list[str], rows: list[list[str]]) -> None:
    """Simple aligned columns: no box drawing, nothing to mangle on Windows."""
    widths = [
        max([len(header)] + [len(row[index]) for row in rows])
        for index, header in enumerate(headers)
    ]
    for line in [headers, *rows]:
        cells = (cell.ljust(w) for cell, w in zip(line, widths, strict=True))
        _out("  ".join(cells).rstrip())


# --------------------------------------------------------------------------
# Settings and the client
# --------------------------------------------------------------------------


def _make_client(settings: Settings) -> Client:
    """Build the client for one command. Tests replace this."""
    return Client(settings.api_key, settings.base_url)


def _settings(*, api_key: str | None = None) -> Settings:
    """Resolve the target, announcing it when it is not production."""
    settings = config.resolve(
        api_key=api_key,
        base_url=_OPTIONS.base_url,
        profile=_OPTIONS.profile,
    )
    if not settings.is_default_target:
        _err(f"Target: {settings.base_url}")
    return settings


def _target_only() -> str:
    """The resolved base URL for a command that needs no key."""
    return _settings(api_key=_UNUSED_KEY).base_url


# --------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------


def _field(label: str, value: Any) -> None:
    _out(f"{label + ':':<14}{value}")


def _or_dash(value: Any) -> str:
    return "-" if value in (None, "") else str(value)


def _render_findings(checks: PublicChecks) -> None:
    if not checks.findings:
        return
    _out("Findings:")
    for finding in checks.findings:
        state = " (dismissed)" if finding.dismissal else ""
        _out(f"  [{finding.check_number}] {finding.severity} {finding.code}{state}")
        _out(f"      {finding.message}")
        if finding.how_to_fix:
            _out(f"      how to fix: {finding.how_to_fix}")
        if finding.doc_url:
            _out(f"      doc: {finding.doc_url}")
        _out(f"      fingerprint: {finding.fingerprint}")


def _render_checks(checks: PublicChecks) -> None:
    _field("Run", checks.run_id)
    _field("Status", checks.status)
    _field(
        "Findings",
        f"{checks.open_findings} open, {checks.dismissed_findings} dismissed",
    )
    _render_findings(checks)


def _render_next(observation: PublicObservation) -> None:
    commands = next_actions_commands(observation)
    if not commands:
        return
    _out("Next:")
    for command in commands:
        _out(f"  {command}")


def _render_readiness(observation: PublicObservation) -> None:
    readiness = observation.readiness
    if readiness is None:
        return
    _field("Readiness", readiness.state)
    if readiness.missing_required:
        _field("Missing", ", ".join(readiness.missing_required))


def _render_observation(observation: PublicObservation) -> None:
    """The one observation view, shared by submit, drafts and observations."""
    metadata = observation.metadata
    _field("Observation", observation.observation_id)
    _field("Status", observation.submission_status)
    _field("Observer", _or_dash(metadata.observer_name))
    _field("Asteroid", _or_dash(metadata.asteroid_name or metadata.asteroid_id))
    _field("Star", _or_dash(metadata.star_id))
    _field("Observed", _or_dash(metadata.observed_at_utc))
    _field("Updated", observation.updated_at)
    _render_readiness(observation)

    present = {slot: file for slot, file in observation.files.items() if file}
    if present:
        _out("Files:")
        for slot, file in present.items():
            _out(f"  {slot:<12}{file.filename} ({file.size_bytes} bytes)")
    if observation.checks is not None:
        _render_checks(observation.checks)
    _render_next(observation)


def _render_event(event: PublicEvent) -> None:
    _field("Event", event.event_id)
    _field("Status", event.status)
    _field("Asteroid", f"{event.asteroid_id} {event.asteroid_name}".strip())
    _field("Star", f"{event.star_catalog} {event.star_id}".strip())
    _field("Date", event.date_window_utc)
    _field("Observers", ", ".join(event.observer_names) or "-")
    _field("Observations", ", ".join(event.observation_ids) or "-")
    _field("Updated", event.updated_at)
    if event.public_notes:
        _field("Notes", event.public_notes)


def _emit_observation(observation: PublicObservation) -> None:
    if _OPTIONS.json:
        _emit(observation.model_dump(mode="json"))
        return
    _render_observation(observation)


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------


def _sub(help_text: str) -> typer.Typer:
    """One sub-app. ``rich_markup_mode=None`` keeps help plain ASCII."""
    return typer.Typer(
        help=help_text,
        no_args_is_help=True,
        rich_markup_mode=None,
        pretty_exceptions_enable=False,
        add_completion=False,
    )


app = _sub("Submit and inspect IOTA Hub occultation observations.")
auth_app = _sub("Store, inspect and forget the API key for a profile.")
drafts_app = _sub("Work on unsubmitted drafts.")
drafts_files_app = _sub("Replace or clear one file slot on a draft.")
observations_app = _sub("Read observations.")
events_app = _sub("Read events.")
files_app = _sub("Download observation or event files.")

app.add_typer(auth_app, name="auth")
app.add_typer(drafts_app, name="drafts")
drafts_app.add_typer(drafts_files_app, name="files")
app.add_typer(observations_app, name="observations")
app.add_typer(events_app, name="events")
app.add_typer(files_app, name="files")


def _version_callback(value: bool) -> None:
    if value:
        _out(f"iota-hub {__version__} (public API v1)")
        raise typer.Exit()


BaseUrlOption = Annotated[
    str | None,
    typer.Option(
        "--base-url",
        metavar="URL",
        help="The API base to talk to. Beats IOTA_HUB_BASE_URL and the profile.",
    ),
]
ProfileOption = Annotated[
    str | None,
    typer.Option(
        "--profile",
        metavar="NAME",
        help="The stored profile to use. Beats IOTA_HUB_PROFILE.",
    ),
]
JsonOption = Annotated[
    bool,
    typer.Option(
        "--json",
        help="Print one JSON document on stdout, and JSON errors on stderr.",
    ),
]


def _shared_options() -> list[inspect.Parameter]:
    """``--base-url``, ``--profile`` and ``--json``, for one command's signature."""
    keyword = inspect.Parameter.KEYWORD_ONLY
    return [
        inspect.Parameter("base_url", keyword, default=None, annotation=BaseUrlOption),
        inspect.Parameter("profile", keyword, default=None, annotation=ProfileOption),
        inspect.Parameter("json", keyword, default=False, annotation=JsonOption),
    ]


IdArgument = Annotated[str, typer.Argument(metavar="ID")]
SlotArgument = Annotated[
    SlotName,
    typer.Argument(metavar="SLOT", help="report, lightcurve, log or vizier."),
]
YesOption = Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")]
TimeoutOption = Annotated[
    float,
    typer.Option("--timeout", metavar="S", help="Seconds to wait for the checks."),
]
NoWaitOption = Annotated[
    bool, typer.Option("--no-wait", help="Do not wait for the check run to finish.")
]
NameFilter = Annotated[
    list[str] | None,
    typer.Option("--observer-name", help="Filter by observer name. Repeatable."),
]
LimitOption = Annotated[int | None, typer.Option("--limit", help="Page size.")]
CursorOption = Annotated[
    str | None, typer.Option("--cursor", help="Continue from this cursor.")
]
AllOption = Annotated[
    bool, typer.Option("--all", help="Follow the cursor and return every match.")
]
SortByOption = Annotated[str | None, typer.Option("--sort-by")]
SortOrderOption = Annotated[str | None, typer.Option("--sort-order")]
DateFromOption = Annotated[str | None, typer.Option("--date-from", metavar="DATE")]
DateToOption = Annotated[str | None, typer.Option("--date-to", metavar="DATE")]


@app.callback()
def root(
    base_url: BaseUrlOption = None,
    profile: ProfileOption = None,
    as_json: JsonOption = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Print the package version and the API version it targets.",
        ),
    ] = False,
) -> None:
    """Submit and inspect IOTA Hub occultation observations.

    The key is never a command-line argument: set IOTA_HUB_API_KEY, or store
    one with `iota-hub auth login`.

    The three options below are also accepted after the subcommand:
    `iota-hub submit DIR --json` is the same run as `iota-hub --json submit DIR`.

    Example: iota-hub submit ./20180305_9721_Doty_Observer_POS --json
    """
    _OPTIONS.base_url = base_url
    _OPTIONS.profile = profile
    _OPTIONS.json = as_json


# --------------------------------------------------------------------------
# submit
# --------------------------------------------------------------------------


@app.command()
@handle_errors
def submit(
    directory: Annotated[
        Path,
        typer.Argument(
            metavar="[DIR]",
            help="The folder holding one observation's files. Defaults to the cwd.",
        ),
    ] = Path("."),
    draft: Annotated[
        bool, typer.Option("--draft", help="Do everything except the final submit.")
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the slot mapping and the target, and call nothing.",
        ),
    ] = False,
    no_wait: Annotated[
        bool,
        typer.Option("--no-wait", help="Return after finalize, without waiting."),
    ] = False,
    timeout: TimeoutOption = POLL_TIMEOUT,
    report: Annotated[
        Path | None,
        typer.Option("--report", help="Use this file for the report slot (.xlsx)."),
    ] = None,
    lightcurve: Annotated[
        Path | None,
        typer.Option("--lightcurve", help="Use this file for the lightcurve (.csv)."),
    ] = None,
    log: Annotated[
        Path | None,
        typer.Option("--log", help="Use this file for the log slot (.txt)."),
    ] = None,
    vizier: Annotated[
        Path | None,
        typer.Option("--vizier", help="Use this file for the vizier slot (.dat)."),
    ] = None,
) -> None:
    """Submit one folder: map, upload, finalize, wait for the checks, submit.

    Exits 4 when the draft came back with open findings or something missing,
    so the whole job is one command a script can branch on.

    Example: iota-hub submit ./20180305_9721_Doty_Observer_POS --draft
    """
    overrides: dict[str, Path | None] = {
        "report": report,
        "lightcurve": lightcurve,
        "log": log,
        "vizier": vizier,
    }
    if dry_run:
        _dry_run(directory, overrides)
        return

    settings = _settings()
    with _make_client(settings) as client:
        result = client.submit_folder(
            directory,
            wait=not no_wait,
            submit=not draft,
            timeout=timeout,
            on_progress=_progress,
            **overrides,
        )
    _emit_submit(result)
    if result.outcome == "needs_attention":
        raise typer.Exit(4)


def _dry_run(directory: Path, overrides: dict[str, Path | None]) -> None:
    """The mapping, the sizes and the target, without touching the network."""
    target = _target_only()
    mapping = map_folder(directory, **overrides)
    uploads = [
        {
            "slot": slot,
            "filename": path.name,
            "path": str(path),
            "size": path.stat().st_size,
        }
        for slot, path in mapping.slots.items()
    ]
    ignored = [path.name for path in mapping.ignored]
    if _OPTIONS.json:
        _emit(
            {
                "target": target,
                "directory": str(mapping.directory),
                "uploads": uploads,
                "ignored": ignored,
            }
        )
        return
    _field("Target", target)
    _field("Directory", mapping.directory)
    _out("Would upload:")
    _table(
        ["  SLOT", "FILE", "BYTES"],
        [
            ["  " + upload["slot"], str(upload["filename"]), str(upload["size"])]
            for upload in uploads
        ],
    )
    if ignored:
        _out(f"Not uploaded (no public slot): {', '.join(ignored)}")


def _emit_submit(result: SubmitResult) -> None:
    ignored = [path.name for path in result.ignored]
    observation = result.observation
    if _OPTIONS.json:
        _emit(
            {
                "observation_id": result.observation_id,
                "outcome": result.outcome,
                "submitted": result.submitted,
                "uploads": result.uploads,
                "ignored": ignored,
                "next_commands": next_actions_commands(observation),
                "observation": observation.model_dump(mode="json"),
            }
        )
        return

    _field("Observation", result.observation_id)
    _field("Outcome", result.outcome)
    _out("Uploaded:")
    for slot, filename in result.uploads.items():
        _out(f"  {slot:<12}{filename}")
    if ignored:
        _out(f"Not uploaded (no public slot): {', '.join(ignored)}")
    if result.outcome == "needs_attention":
        _render_readiness(observation)
        if observation.checks is not None:
            _render_findings(observation.checks)
    _render_next(observation)


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------


@auth_app.command("login")
@handle_errors
def auth_login() -> None:
    """Read an API key and store it, with its base URL, in a profile.

    The key is read from a hidden prompt on a terminal, otherwise from one line
    on stdin -- never from a command-line argument, which would land in the
    shell history and in `ps`.

    Example: iota-hub auth login --profile dev --base-url https://host/dev/api
    """
    name = _OPTIONS.profile or "default"
    stored = (config.load_config().get("profiles") or {}).get(name) or {}
    target = (
        _OPTIONS.base_url
        or os.environ.get(ENV_BASE_URL)
        or stored.get("base_url")
        or DEFAULT_BASE_URL
    ).rstrip("/")
    if target != DEFAULT_BASE_URL:
        _err(f"Target: {target}")

    key = _read_key()
    if not key.startswith("iotahub_"):
        raise AuthError(
            "malformed_api_key",
            "That does not look like an IOTA Hub key.",
            hint="A key looks like iotahub_<key_id>_<secret>.",
        )

    settings = Settings(
        api_key=key,
        base_url=target,
        profile_name=name,
        source_of_key="flag",
        is_default_target=target == DEFAULT_BASE_URL,
    )
    with _make_client(settings) as client:
        client.list_observations(limit=1)
    path = config.save_profile(name, api_key=key, base_url=target)

    prefix = config.key_prefix(key)
    if _OPTIONS.json:
        _emit(
            {
                "profile": name,
                "base_url": target,
                "key_prefix": prefix,
                "config_file": str(path),
            }
        )
        return
    _field("Profile", name)
    _field("Base URL", target)
    _field("Key", prefix)
    _field("Saved to", path)


def _read_key() -> str:
    """The key, from a hidden prompt or from stdin. Never echoed, ever."""
    if sys.stdin.isatty():
        # The prompt is not data: stdout carries only the command's output.
        return typer.prompt("API key", hide_input=True, err=True).strip()
    key = sys.stdin.readline().strip()
    if not key:
        raise AuthError(
            "missing_api_key",
            "Nothing on stdin to read a key from.",
            hint="Pipe the key in, or run `iota-hub auth login` on a terminal.",
        )
    return key


@auth_app.command("status")
@handle_errors
def auth_status() -> None:
    """Show what is configured, and check the key against the API.

    It reports the check rather than failing on it, so it exits 0 either way.

    Example: iota-hub auth status --profile dev
    """
    settings = _settings()
    with _make_client(settings) as client:
        try:
            client.list_observations(limit=1)
            check = "valid"
        except IotaHubError as error:
            check = error.code

    path = config.config_file_path()
    prefix = config.key_prefix(settings.api_key)
    if _OPTIONS.json:
        _emit(
            {
                "profile": settings.profile_name,
                "base_url": settings.base_url,
                "key_prefix": prefix,
                "key_source": settings.source_of_key,
                "config_file": str(path),
                "check": check,
            }
        )
        return
    _field("Profile", _or_dash(settings.profile_name))
    _field("Base URL", settings.base_url)
    _field("Key", prefix)
    _field("Key source", settings.source_of_key)
    _field("Config file", path)
    _field("Check", check)


@auth_app.command("logout")
@handle_errors
def auth_logout() -> None:
    """Forget one profile's stored key and base URL.

    Example: iota-hub auth logout --profile dev
    """
    name = _OPTIONS.profile or "default"
    deleted = config.delete_profile(name)
    path = config.config_file_path()
    if _OPTIONS.json:
        _emit({"profile": name, "deleted": deleted, "config_file": str(path)})
        return
    if deleted:
        _out(f"Removed profile '{name}' from {path}")
    else:
        _out(f"No profile named '{name}' in {path}")


# --------------------------------------------------------------------------
# drafts
# --------------------------------------------------------------------------


@drafts_app.command("list")
@handle_errors
def drafts_list() -> None:
    """List every unsubmitted draft, following the cursor to the end.

    Example: iota-hub drafts list
    """
    settings = _settings()
    with _make_client(settings) as client:
        drafts = list(client.iter_observations(submission_status="draft"))

    if _OPTIONS.json:
        _emit({"items": [draft.model_dump(mode="json") for draft in drafts]})
        return
    if not drafts:
        _out("No drafts.")
        return
    rows = []
    for draft in drafts:
        readiness = draft.readiness
        rows.append(
            [
                draft.observation_id,
                readiness.state if readiness else "-",
                str(readiness.open_findings) if readiness else "-",
                str(len(readiness.missing_required)) if readiness else "-",
                draft.updated_at,
            ]
        )
    _table(["OBSERVATION", "READINESS", "OPEN", "MISSING", "UPDATED"], rows)


@drafts_app.command("show")
@handle_errors
def drafts_show(observation_id: IdArgument) -> None:
    """Show one draft with its readiness, findings and next commands.

    Example: iota-hub drafts show obs_01JABCDEF
    """
    settings = _settings()
    with _make_client(settings) as client:
        observation = client.get_observation(observation_id)
    _emit_observation(observation)


@drafts_app.command("check")
@handle_errors
def drafts_check(
    observation_id: IdArgument,
    no_wait: NoWaitOption = False,
    timeout: TimeoutOption = POLL_TIMEOUT,
) -> None:
    """Start a fresh observer check run on a draft and wait for it.

    Example: iota-hub drafts check obs_01JABCDEF
    """
    settings = _settings()
    with _make_client(settings) as client:
        try:
            client.run_checks(observation_id)
        except ConflictError as error:
            # A run that is already under way is the state this command wanted
            # anyway, so wait for that one instead of failing (API spec: 409
            # check_run_in_progress says to poll rather than start another).
            if error.code != "check_run_in_progress":
                raise
            if no_wait:
                _err(f"A check run is already under way for {observation_id}.")
        if no_wait:
            observation = client.get_observation(observation_id)
        else:
            observation = client.wait_for_checks(
                observation_id, timeout=timeout, on_progress=_progress
            )
    _emit_observation(observation)


@drafts_app.command("submit")
@handle_errors
def drafts_submit(observation_id: IdArgument) -> None:
    """Submit a draft that is ready. Exits 4, with the findings, when it is not.

    Example: iota-hub drafts submit obs_01JABCDEF
    """
    settings = _settings()
    with _make_client(settings) as client:
        observation = client.get_observation(observation_id)
        if observation.submission_status == "submitted":
            raise IotaHubError(
                "already_submitted",
                f"{observation_id} is already submitted.",
                hint=(
                    "This observation is already submitted; submitted "
                    "observations cannot be re-submitted."
                ),
                details={"observation_id": observation_id},
            )
        readiness = observation.readiness
        if readiness is None or readiness.state != "ready":
            if not _OPTIONS.json:
                _render_observation(observation)
            raise _not_ready(observation)
        observation = client.submit_draft(
            observation_id,
            version=observation.version,
            idempotency_key=str(uuid.uuid4()),
        )
    _emit_observation(observation)


def _not_ready(observation: PublicObservation) -> IotaHubError:
    """The refusal the API would give, decided from the readiness just read.

    Saves a write that is certain to be rejected, and reports the same ``code``
    the API's own refusal carries (specs/public-api.md section 8), so a script
    sees one contract whichever path it took.
    """
    readiness = observation.readiness
    if readiness is None:
        code, why = "checks_stale", "it has no readiness yet"
    elif readiness.missing_required:
        code = "missing_required"
        why = f"it is still missing {', '.join(readiness.missing_required)}"
    elif readiness.event_files_blocked:
        code, why = "event_files_conflict", "staged event files would collide"
    elif readiness.open_findings:
        code = "open_findings"
        why = f"it has {readiness.open_findings} open finding(s)"
    else:
        code, why = "checks_stale", "its checks do not describe it as it stands"
    observation_id = observation.observation_id
    return ConflictError(
        code,
        f"{observation_id} is not ready to submit: {why}.",
        hint=f"See `iota-hub drafts show {observation_id}` for what is left.",
        details={
            "observation_id": observation_id,
            "readiness": readiness.model_dump(mode="json") if readiness else None,
        },
    )


@drafts_app.command("delete")
@handle_errors
def drafts_delete(observation_id: IdArgument, yes: YesOption = False) -> None:
    """Throw away an unsubmitted draft. A submitted observation cannot be deleted.

    Example: iota-hub drafts delete obs_01JABCDEF --yes
    """
    _confirm(f"Delete draft {observation_id}?", yes)
    settings = _settings()
    with _make_client(settings) as client:
        client.delete_draft(observation_id)
    if _OPTIONS.json:
        _emit({"observation_id": observation_id, "deleted": True})
        return
    _out(f"Deleted draft {observation_id}")


def _confirm(question: str, yes: bool) -> None:
    """Ask before something destructive -- and never ask a pipe."""
    if yes:
        return
    if not sys.stdin.isatty():
        raise IotaHubError(
            "confirmation_required",
            f"{question} Refusing to assume an answer: stdin is not a terminal.",
            hint="Pass --yes to confirm without being asked.",
        )
    typer.confirm(question, abort=True, err=True)


@drafts_app.command("dismiss")
@handle_errors
def drafts_dismiss(
    observation_id: IdArgument,
    fingerprint: Annotated[str, typer.Argument(metavar="FINGERPRINT")],
    note: Annotated[
        str | None,
        typer.Option("--note", help="Why the finding is expected. Needed to dismiss."),
    ] = None,
    undo: Annotated[
        bool, typer.Option("--undo", help="Undo a dismissal instead of making one.")
    ] = False,
) -> None:
    """Dismiss one finding with a note, or undo that dismissal.

    Example: iota-hub drafts dismiss obs_01JABCDEF 9f2a1c --note "Known offset"
    """
    if not undo and not note:
        # A missing flag is a usage error, and typer already owns those: it
        # prints the usage line and exits 2.
        raise typer.BadParameter(
            "a dismissal needs a note saying why the finding is expected; "
            "pass --note, or --undo to undo one instead.",
            param_hint="--note",
        )
    settings = _settings()
    with _make_client(settings) as client:
        if undo:
            checks = client.undo_dismissal(observation_id, fingerprint)
        else:
            checks = client.dismiss_finding(observation_id, fingerprint, note=note)
    if _OPTIONS.json:
        _emit(checks.model_dump(mode="json"))
        return
    _render_checks(checks)


@drafts_files_app.command("add")
@handle_errors
def drafts_files_add(
    observation_id: IdArgument,
    slot: SlotArgument,
    path: Annotated[Path, typer.Argument(metavar="PATH")],
) -> None:
    """Put a new file in one slot of a draft: init, upload, finalize.

    Example: iota-hub drafts files add obs_01JABCDEF lightcurve ./fixed.csv
    """
    settings = _settings()
    with _make_client(settings) as client:
        observation = client.replace_file(
            observation_id, slot.value, path, on_progress=_progress
        )
    _emit_observation(observation)


@drafts_files_app.command("rm")
@handle_errors
def drafts_files_rm(
    observation_id: IdArgument,
    slot: SlotArgument,
    yes: YesOption = False,
) -> None:
    """Clear one file slot on a draft.

    Example: iota-hub drafts files rm obs_01JABCDEF vizier --yes
    """
    _confirm(f"Remove the {slot.value} file from {observation_id}?", yes)
    settings = _settings()
    with _make_client(settings) as client:
        observation = client.delete_file(observation_id, slot.value)
    _emit_observation(observation)


# --------------------------------------------------------------------------
# observations
# --------------------------------------------------------------------------


@observations_app.command("list")
@handle_errors
def observations_list(
    observer_name: NameFilter = None,
    event_status_group: Annotated[
        str | None, typer.Option("--event-status-group")
    ] = None,
    submission_status: Annotated[
        str | None, typer.Option("--submission-status")
    ] = None,
    asteroid_id: Annotated[str | None, typer.Option("--asteroid-id")] = None,
    asteroid_name: Annotated[str | None, typer.Option("--asteroid-name")] = None,
    star_id: Annotated[str | None, typer.Option("--star-id")] = None,
    result_type: Annotated[
        list[str] | None,
        typer.Option("--result-type", help="Filter by result type. Repeatable."),
    ] = None,
    ungrouped_only: Annotated[
        bool,
        typer.Option("--ungrouped-only", help="Only ones not attached to an event."),
    ] = False,
    date_from: DateFromOption = None,
    date_to: DateToOption = None,
    sort_by: SortByOption = None,
    sort_order: SortOrderOption = None,
    limit: LimitOption = None,
    cursor: CursorOption = None,
    follow_all: AllOption = False,
) -> None:
    """List observations with the web app's filters.

    Example: iota-hub observations list --submission-status submitted --all
    """
    filters: dict[str, Any] = {
        "observer_names": list(observer_name) if observer_name else None,
        "event_status_group": event_status_group,
        "submission_status": submission_status,
        "asteroid_id": asteroid_id,
        "asteroid_name": asteroid_name,
        "star_id": star_id,
        "result_type": list(result_type) if result_type else None,
        "ungrouped_only": True if ungrouped_only else None,
        "date_from": date_from,
        "date_to": date_to,
        "sort_by": sort_by,
        "sort_order": sort_order,
        "limit": limit,
    }
    settings = _settings()
    with _make_client(settings) as client:
        if follow_all:
            items = list(client.iter_observations(cursor=cursor, **filters))
            next_cursor = None
        else:
            page = client.list_observations(cursor=cursor, **filters)
            items, next_cursor = page.items, page.next_cursor

    if _OPTIONS.json:
        _emit(
            {
                "items": [item.model_dump(mode="json") for item in items],
                "next_cursor": next_cursor,
            }
        )
        return
    if not items:
        _out("No observations.")
        return
    rows = [
        [
            item.observation_id,
            item.submission_status,
            _or_dash(item.metadata.asteroid_name or item.metadata.asteroid_id),
            _or_dash(item.metadata.star_id),
            _or_dash(item.metadata.observed_at_utc),
            item.updated_at,
        ]
        for item in items
    ]
    _table(["OBSERVATION", "STATUS", "ASTEROID", "STAR", "OBSERVED", "UPDATED"], rows)
    if next_cursor:
        _err(f"More results: pass --cursor {next_cursor} (or --all).")


@observations_app.command("show")
@handle_errors
def observations_show(observation_id: IdArgument) -> None:
    """Show one observation.

    Example: iota-hub observations show obs_01JABCDEF
    """
    settings = _settings()
    with _make_client(settings) as client:
        observation = client.get_observation(observation_id)
    _emit_observation(observation)


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------


@events_app.command("list")
@handle_errors
def events_list(
    status: Annotated[
        str | None,
        typer.Option("--status", help="One status, or several comma-separated (OR)."),
    ] = None,
    asteroid_id: Annotated[str | None, typer.Option("--asteroid-id")] = None,
    asteroid_name: Annotated[str | None, typer.Option("--asteroid-name")] = None,
    star_id: Annotated[str | None, typer.Option("--star-id")] = None,
    date_from: DateFromOption = None,
    date_to: DateToOption = None,
    observer_name: NameFilter = None,
    waiting_for: Annotated[str | None, typer.Option("--waiting-for")] = None,
    assigned_reviewer_id: Annotated[
        list[str] | None,
        typer.Option("--assigned-reviewer-id", help="Repeatable."),
    ] = None,
    sort_by: SortByOption = None,
    sort_order: SortOrderOption = None,
    limit: LimitOption = None,
    cursor: CursorOption = None,
    follow_all: AllOption = False,
) -> None:
    """List events with the web app's filters.

    Example: iota-hub events list --status COMPLETE,DELIVERED --all
    """
    filters: dict[str, Any] = {
        "status": status,
        "asteroid_id": asteroid_id,
        "asteroid_name": asteroid_name,
        "star_id": star_id,
        "date_from": date_from,
        "date_to": date_to,
        "observer_names": list(observer_name) if observer_name else None,
        "waiting_for": waiting_for,
        "assigned_reviewer_id": (
            list(assigned_reviewer_id) if assigned_reviewer_id else None
        ),
        "sort_by": sort_by,
        "sort_order": sort_order,
        "limit": limit,
    }
    settings = _settings()
    with _make_client(settings) as client:
        if follow_all:
            items = list(client.iter_events(cursor=cursor, **filters))
            next_cursor = None
        else:
            page = client.list_events(cursor=cursor, **filters)
            items, next_cursor = page.items, page.next_cursor

    if _OPTIONS.json:
        _emit(
            {
                "items": [item.model_dump(mode="json") for item in items],
                "next_cursor": next_cursor,
            }
        )
        return
    if not items:
        _out("No events.")
        return
    rows = [
        [
            item.event_id,
            item.status,
            f"{item.asteroid_id} {item.asteroid_name}".strip(),
            item.star_id,
            item.date_window_utc,
            str(len(item.observation_ids)),
        ]
        for item in items
    ]
    _table(["EVENT", "STATUS", "ASTEROID", "STAR", "DATE", "OBS"], rows)
    if next_cursor:
        _err(f"More results: pass --cursor {next_cursor} (or --all).")


@events_app.command("show")
@handle_errors
def events_show(event_id: Annotated[str, typer.Argument(metavar="ID")]) -> None:
    """Show one event.

    Example: iota-hub events show evt_01JABCDEF
    """
    settings = _settings()
    with _make_client(settings) as client:
        event = client.get_event(event_id)
    if _OPTIONS.json:
        _emit(event.model_dump(mode="json"))
        return
    _render_event(event)


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------


@files_app.command("download")
@handle_errors
def files_download(
    kind: Annotated[ResourceKind, typer.Argument(metavar="observation|event")],
    resource_id: IdArgument,
    slot: Annotated[
        str | None,
        typer.Option(
            "--slot",
            metavar="SLOT",
            help=(
                "Download only this slot. Any slot the server names, not just "
                "the four upload slots: damit, ground_track, attachment, ..."
            ),
        ),
    ] = None,
    out_dir: Annotated[
        Path,
        typer.Option("-o", "--out", metavar="DIR", help="Where to write the files."),
    ] = Path("."),
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite files that are already there.")
    ] = False,
) -> None:
    """Download an observation's or an event's files with presigned links.

    Example: iota-hub files download observation obs_01JABCDEF -o ./downloads
    """
    settings = _settings()
    with _make_client(settings) as client:
        written = client.download_files(
            kind.value,
            resource_id,
            out_dir,
            slot=slot,
            overwrite=force,
            on_progress=_progress,
        )
    if _OPTIONS.json:
        _emit({"files": [str(path) for path in written]})
        return
    for path in written:
        _out(str(path))


# --------------------------------------------------------------------------
# guide
# --------------------------------------------------------------------------


@app.command()
@handle_errors
def guide() -> None:
    """Print the bundled CLI workflow guide.

    Example: iota-hub guide
    """
    try:
        text = (
            resource_files("iota_hub").joinpath("guide.md").read_text(encoding="utf-8")
        )
    except OSError:
        # FileNotFoundError is an OSError: a build without the guide still
        # answers, with the URL the guide lives at.
        text = GUIDE_FALLBACK
    if _OPTIONS.json:
        _emit({"guide": text})
        return
    _out(text.rstrip("\n"))


def main() -> None:
    """The ``iota-hub`` entry point."""
    for stream in (sys.stdout, sys.stderr):
        # API data can carry characters a Windows console's code page cannot
        # encode; escape them rather than dying with UnicodeEncodeError.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")
    # ``standalone_mode=False`` hands click's own usage errors back here, so
    # they can be JSON too; the exit code is returned instead of raised.
    try:
        code = app(standalone_mode=False)
    except UsageError as error:
        _usage_error(error)
    except typer.Abort:
        _err("Aborted!")
        raise SystemExit(1) from None
    raise SystemExit(code if isinstance(code, int) else 0)


def _usage_error(error: UsageError) -> NoReturn:
    """A bad invocation: click's own message, as JSON when --json was asked for.

    ``--json`` is read from the argument list because the error happened while
    parsing it -- no callback ran, so nothing settled :data:`_OPTIONS`.
    """
    if "--json" in sys.argv:
        _err(
            json.dumps(
                {
                    "code": "usage_error",
                    "message": error.format_message(),
                    "hint": "Run the command with --help.",
                    "details": {},
                    "status": None,
                    "retry_after": None,
                    "exit_code": 2,
                },
                indent=2,
            )
        )
    else:
        error.show()
    raise SystemExit(error.exit_code)


if __name__ == "__main__":
    main()
