"""The ``iota-hub`` command: a thin layer over the library.

Every decision this module makes is about *presentation* -- which stream a line
goes to, how an error becomes an exit code, what a ``--json`` document looks
like. The work itself belongs to :mod:`iota_hub.workflow` and
:mod:`iota_hub.client` (design section 8.1: "the ``iota-hub`` command as a thin
layer on it"), so an embedder gets the same behavior without the CLI.

The conventions are ``docs/client-conventions.md`` sections 12-13: data on
stdout, progress and ``Target:`` on stderr, one JSON document on stdout under
``--json`` and nothing but a problem-details object on stderr when it fails,
documented exit codes, plain ASCII unless a UTF-8 terminal is styled
(:mod:`._term`), and never a prompt when stdin is not a TTY.
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
from rich.text import Text

try:  # typer 0.27+ vendors click; older typer depends on the package.
    from typer._click.exceptions import UsageError
except ImportError:  # pragma: no cover - typer < 0.27 only
    from click.exceptions import UsageError  # type: ignore[no-redef]

from . import _term, config
from ._http import DEFAULT_BASE_URL
from ._version import __version__
from .client import Client
from .config import ENV_BASE_URL, Settings
from .errors import AuthError, ConflictError, IotaHubError, RateLimitError
from .files import map_folder
from .models import (
    PublicCheckResponse,
    PublicChecks,
    PublicEvent,
    PublicFinding,
    PublicObservation,
    PublicObservationMetadata,
)
from .workflow import (
    POLL_TIMEOUT,
    SubmitResult,
    candidate_line,
    check_warnings,
    next_actions_commands,
)

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
        "blocked_attachment",
        "attachment_too_large",
        "too_many_attachments",
        "unknown_profile",
        "confirmation_required",
        "not_writable",
        # The API's own: the replacement report describes another observation.
        # Nothing is wrong with the observation; the file the caller named is
        # the wrong one for it, like an ``invalid_extension``.
        "identity_changed",
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
    """The file slots the public API accepts, plus ``attachment``."""

    report = "report"
    lightcurve = "lightcurve"
    log = "log"
    vizier = "vizier"
    aperture_record = "aperture_record"
    attachment = "attachment"


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
            _HELD_NOTES[:] = []
            _fail(error)
        except typer.Abort:
            # A prompt declined or cancelled. Plain output keeps main()'s
            # "Aborted!"; --json still owes its one error document.
            _HELD_NOTES[:] = []
            if not _OPTIONS.json:
                raise
            _fail(
                IotaHubError(
                    "aborted",
                    "Stopped at a prompt: it was declined or cancelled.",
                    hint=(
                        "Run the command again; --yes skips a confirmation on "
                        "the commands that ask for one."
                    ),
                )
            )
        finally:
            for line in _HELD_NOTES:
                _err(line)

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
    _term.plain = _OPTIONS.json
    _LAST_PROGRESS[:] = []
    _HELD_NOTES[:] = []


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
    elif _term.styled(err=True):
        _term.show(
            _term.text(
                (f"{_term.glyph('fail', True)} error ", "bold red"),
                (error.code, "bold"),
                f": {error.message}",
            ),
            err=True,
        )
        if error.hint:
            _term.show(_term.text(("  hint  ", "dim"), error.hint), err=True)
        if error.details:
            details = json.dumps(error.details)
            _term.show(
                _term.text(("  details  ", "dim"), details), err=True, soft_wrap=True
            )
        if error.retry_after is not None:
            _term.show(Text(f"  retry after {error.retry_after:g} s", "dim"), err=True)
    else:
        _err(f"error: {error.code}: {error.message}")
        if error.hint:
            _err(f"hint: {error.hint}")
        if error.details:
            _err(f"details: {json.dumps(error.details)}")
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


#: Under ``--json``, the stderr lines of a run still in progress. A failed run's
#: stderr is one JSON document (conventions section 13), and whether it fails
#: is not known until it ends: they are printed when it succeeds, dropped when
#: it does not.
_HELD_NOTES: list[str] = []


def _note(line: str) -> None:
    """One line of progress or targeting, held back under ``--json``."""
    if _OPTIONS.json:
        _HELD_NOTES.append(line)
    else:
        _err(line)


def _emit(payload: Any) -> None:
    """The one JSON document a ``--json`` run prints."""
    _out(json.dumps(payload, indent=2))


#: The last progress line printed, so a repeated one (each poll says "Waiting
#: for checks...") is printed once.
_LAST_PROGRESS: list[str] = []


def _progress(message: str) -> None:
    if [message] == _LAST_PROGRESS:
        return
    _LAST_PROGRESS[:] = [message]
    _diagnostic(message)


def _diagnostic(line: str) -> None:
    """One stderr line: a progress step, or a line of the ``check_warnings`` block.

    Plain, it is printed as is. Styled, a progress step is dimmed, and the
    warning block (``WARNING:`` lines and their indented detail) stands out.
    """
    if not _term.styled(err=True):
        _note(line)
        return
    if line.startswith("WARNING:"):
        _err("")
        shown = Text(line, "bold yellow")
    elif line.lstrip().startswith("iota-hub "):
        shown = Text(line, "cyan")
    elif line.startswith(" "):
        shown = Text(line)
    else:
        shown = Text(f"{_term.glyph('step', True)} {line}", "dim")
    _term.show(shown, err=True, soft_wrap=True)


def _table(headers: list[str], rows: list[list[str]]) -> None:
    """Simple aligned columns: no box drawing, nothing to mangle on Windows."""
    widths = [
        max([len(header)] + [len(row[index]) for row in rows])
        for index, header in enumerate(headers)
    ]
    styled = _term.styled()
    for index, line in enumerate([headers, *rows]):
        cells = [cell.ljust(w) for cell, w in zip(line, widths, strict=True)]
        if not styled:
            _out("  ".join(cells).rstrip())
        elif index == 0:
            _term.show(Text("  ".join(cells).rstrip(), "bold"), soft_wrap=True)
        else:
            # The first column is what the row is about: a slot, an id.
            rest = "  ".join(cells[1:]).rstrip()
            row = _term.text((cells[0], _ACCENT), "  " + rest if rest else "")
            _term.show(row, soft_wrap=True)


def _render_files(
    rows: list[tuple[str, str, int | None]], title: str = "Files"
) -> None:
    """The one files section: ``  <slot>  <filename>  (<size>)``.

    Shared by ``submit``, ``submit --dry-run`` and the observation view, so a
    dry run reads like the real thing.
    """
    if not rows:
        return
    _heading(title)
    width = max(12, *(len(slot) + 2 for slot, _, _ in rows))
    for slot, filename, size in rows:
        rest = filename if size is None else f"{filename}  ({_size(size)})"
        if _term.styled():
            line = _term.text((f"  {slot:<{width}}", _ACCENT), rest)
            _term.show(line, soft_wrap=True)
        else:
            _out(f"  {slot:<{width}}{rest}")


# --------------------------------------------------------------------------
# Settings and the client
# --------------------------------------------------------------------------


def _make_client(settings: Settings) -> Client:
    """Build the client for one command. Tests replace this."""
    return Client(settings.api_key, settings.base_url)


def _settings(*, api_key: str | None = None, always_announce: bool = False) -> Settings:
    """Resolve the target, announcing it when it is not production (or always)."""
    settings = config.resolve(
        api_key=api_key,
        base_url=_OPTIONS.base_url,
        profile=_OPTIONS.profile,
    )
    if always_announce or not settings.is_default_target:
        if _term.styled(err=True):
            fallback = "production" if settings.is_default_target else "not production"
            badge = (settings.profile_name or fallback).upper()
            _term.show(
                _term.text(
                    (f" {badge} ", "bold black on yellow"),
                    " ",
                    (settings.base_url, "yellow"),
                ),
                err=True,
                soft_wrap=True,
            )
        else:
            _note(f"Target: {settings.base_url}")
    return settings


# --------------------------------------------------------------------------
# Renderers
#
# One set of facts, two looks: plain text (pipes, NO_COLOR, the tests) and,
# on a terminal, the same lines with color, spacing and wrapping (_term).
# --------------------------------------------------------------------------

#: Values that read as an outcome: (glyph, color).
_STATUS_STYLE = {
    "submitted": ("ok", "green"),
    "ready": ("ok", "green"),
    "complete": ("ok", "green"),
    "valid": ("ok", "green"),
    "needs_attention": ("warn", "yellow"),
    "timeout": ("warn", "yellow"),
    "error": ("fail", "red"),
}

#: The one accent color: section headings, slots, the first column of a table.
_ACCENT = "bright_blue"

_SEVERITY_STYLE = {"error": "bold red", "warning": "yellow"}

#: Wide enough for the longest label, so separate rows still line up.
_LABEL_WIDTH = 12


def _status_text(value: Any) -> Text:
    word = str(value)
    style = _STATUS_STYLE.get(word)
    if style is None:
        return Text(word, "bold")
    glyph, color = style
    return Text(f"{_term.glyph(glyph)} {word}", f"bold {color}")


def _field(label: str, value: Any, *, status: bool = False, style: str = "") -> None:
    if _term.styled():
        shown = _status_text(value) if status else Text(str(value), style)
        # One unbroken line: a value is often a URL or an id, which must stay
        # clickable and copyable, so the terminal wraps it, not rich.
        label_text = (label.ljust(_LABEL_WIDTH) + "  ", "dim")
        _term.show(_term.text(label_text, shown), soft_wrap=True)
        return
    _out(f"{label + ':':<14}{value}")


def _heading(title: str) -> None:
    if _term.styled():
        _out()
        _term.show(Text(title, f"bold {_ACCENT}"))
        return
    _out(f"{title}:")


def _command(command: str) -> None:
    """One copy-pasteable command, indented under its heading."""
    if _term.styled():
        _term.show(Text(command, "cyan"), indent=2, soft_wrap=True)
        return
    _out(f"  {command}")


def _pasteable(path: Path) -> str:
    """A path a person can paste into a shell: double-quoted if it has a space.

    macOS keeps the config under "Application Support". Double quotes, not
    ``shlex.quote``'s single ones, because cmd.exe only understands double.
    """
    text = str(path)
    return f'"{text}"' if " " in text else text


def _size(size_bytes: int) -> str:
    """A file size as the web app shows it: B, then KB, then MB (base 1024)."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _or_dash(value: Any) -> str:
    return "-" if value in (None, "") else str(value)


def _joined(*parts: str | None) -> str:
    """``UCAC4 326-152286``: the parts there are, or ``-``."""
    return _or_dash(" ".join(part for part in parts if part))


def _asteroid(number: str | None, name: str | None) -> str:
    """``(33777) 1999 RM158``, as the web app writes it; ``-`` when unknown."""
    return _joined(f"({number})" if number else None, name)


def _render_identity(metadata: PublicObservationMetadata) -> None:
    """What was observed, as the server parsed it from the report."""
    _field("Observer", _or_dash(metadata.observer_name))
    _field("Asteroid", _asteroid(metadata.asteroid_id, metadata.asteroid_name))
    _field("Star", _joined(metadata.star_catalog, metadata.star_id))
    _field("Observed", _or_dash(metadata.observed_at_utc))


def _render_findings(checks: PublicChecks) -> None:
    if not checks.findings:
        return
    if _term.styled():
        _render_findings_styled(checks.findings)
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


def _finding_group(finding: PublicFinding) -> tuple[Any, ...]:
    return (
        finding.check_number,
        finding.code,
        finding.how_to_fix,
        finding.doc_url,
        finding.dismissal is None,
    )


def _render_findings_styled(findings: list[PublicFinding]) -> None:
    """Findings on a terminal: one block per check, the fix said once."""
    groups: list[list[PublicFinding]] = []
    for finding in findings:
        if groups and _finding_group(groups[-1][0]) == _finding_group(finding):
            groups[-1].append(finding)
        else:
            groups.append([finding])

    _heading("Findings")
    for index, group in enumerate(groups):
        first = group[0]
        dismissed = first.dismissal is not None
        count = f" ({len(group)})" if len(group) > 1 else ""
        if index:
            _out()
        _term.show(
            _term.text(
                (f"[{first.check_number}] ", "dim"),
                (
                    first.severity,
                    "dim" if dismissed else _SEVERITY_STYLE.get(first.severity, ""),
                ),
                "  ",
                (first.code + count, "dim" if dismissed else "bold"),
                (" (dismissed)" if dismissed else "", "dim"),
            ),
            indent=2,
        )
        for finding in group:
            _term.show(Text(finding.message), indent=6)
        if first.how_to_fix:
            _term.pairs([("Fix", Text(first.how_to_fix))], indent=6)
        # A link and ids are never wrapped by rich: they stay clickable.
        if first.doc_url:
            _term.show(
                _term.text(("Doc  ", "dim"), first.doc_url), soft_wrap=True, indent=6
            )
        fingerprints = ", ".join(finding.fingerprint for finding in group)
        _term.show(
            _term.text(("ID   ", "dim"), (fingerprints, "dim")),
            soft_wrap=True,
            indent=6,
        )


def _render_checks(checks: PublicChecks) -> None:
    _field("Run", checks.run_id)
    _field("Status", checks.status, status=True)
    _field(
        "Findings",
        f"{checks.open_findings} open, {checks.dismissed_findings} dismissed",
    )
    _render_findings(checks)
    if checks.skipped:
        _heading("Skipped (did not run)")
        for check in checks.skipped:
            _out(
                f"  [{check.check_number}] {check.name} ({check.code}): {check.reason}"
            )


def _render_owc(observation: PublicObservation) -> None:
    owc = observation.owc
    if owc is None:
        return
    if owc.owc_event_id:
        _field("OWC", f"{owc.choice}: {owc.owc_event_url or owc.owc_event_id}")
    elif owc.choice == "proceed_unlinked":
        _field("OWC", "no link (proceed_unlinked)")
    elif owc.resolution is not None:
        reason = f" - {owc.resolution.reason}" if owc.resolution.reason else ""
        _field("OWC", f"unresolved ({owc.resolution.status}){reason}")
        for candidate in owc.resolution.candidates:
            _out(f"  {candidate_line(candidate)}")


def _warn_checks(observation: PublicObservation) -> None:
    """The loud block after a wait (``check_warnings``), on stderr in every mode.

    A diagnostic like the progress lines, so a ``--json`` stdout stays one
    document -- which carries the same facts. Warns, never blocks.
    """
    for line in check_warnings(observation):
        _diagnostic(line)


def _render_next(observation: PublicObservation) -> None:
    commands = next_actions_commands(observation)
    if not commands:
        return
    _heading("Next")
    for command in commands:
        _command(command)


def _render_readiness(observation: PublicObservation, *, unless: str = "") -> None:
    """Readiness, leaving out a state that repeats ``unless`` (submit's outcome)."""
    readiness = observation.readiness
    if readiness is None:
        return
    if readiness.state != unless:
        _field("Readiness", readiness.state, status=True)
    if readiness.missing_required:
        _field("Missing", ", ".join(readiness.missing_required), style="bold red")


def _render_observation(observation: PublicObservation) -> None:
    """The one observation view, shared by drafts and observations."""
    _field("Observation", observation.observation_id, style="bold")
    _field("Status", observation.status, status=True)
    if observation.amendment_count:
        _field(
            "Amended",
            f"{observation.amendment_count} time(s), last {observation.amended_at}",
        )
    if observation.web_url:
        _field("Web", observation.web_url)
    _render_identity(observation.metadata)
    _field("Updated", observation.updated_at)
    _render_readiness(observation)
    _render_owc(observation)

    _render_files(
        [
            (slot, file.filename, file.size_bytes)
            for slot, file in observation.files.items()
            if file
        ]
    )
    if observation.attachments:
        _heading("Attachments")
        for attachment in observation.attachments:
            _out(
                f"  {attachment.attachment_id}  {attachment.filename}"
                f"  ({_size(attachment.size_bytes)})"
            )
    if observation.checks is not None:
        if _term.styled():
            _out()
        _render_checks(observation.checks)
    _render_next(observation)


def _render_event(event: PublicEvent) -> None:
    _field("Event", event.event_id, style="bold")
    _field("Status", event.status, status=True)
    _field("Asteroid", _asteroid(event.asteroid_id, event.asteroid_name))
    _field("Star", _joined(event.star_catalog, event.star_id))
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
drafts_files_app = _sub(
    "Replace or clear one file slot on a draft, or add or remove an attachment. "
    "On a submitted observation, replacing a file is how its owner amends it."
)
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
    typer.Argument(
        metavar="SLOT",
        help="report, lightcurve, log, vizier, aperture_record or attachment.",
    ),
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
        typer.Option(
            "--report",
            help="Use this file for the report slot (.xlsx, .xls or .json).",
        ),
    ] = None,
    lightcurve: Annotated[
        Path | None,
        typer.Option("--lightcurve", help="Use this file for the lightcurve (.csv)."),
    ] = None,
    log: Annotated[
        Path | None,
        typer.Option(
            "--log",
            help="Use this file for the log slot (any .txt, whatever its name).",
        ),
    ] = None,
    vizier: Annotated[
        Path | None,
        typer.Option("--vizier", help="Use this file for the vizier slot (.dat)."),
    ] = None,
    aperture_record: Annotated[
        Path | None,
        typer.Option(
            "--aperture-record",
            help="Use this file for the aperture_record slot (.lc or .pymovie).",
        ),
    ] = None,
    attach: Annotated[
        list[Path] | None,
        typer.Option(
            "--attach",
            metavar="PATH",
            help="Also upload this file as an attachment. Repeatable.",
        ),
    ] = None,
    no_attachments: Annotated[
        bool,
        typer.Option(
            "--no-attachments",
            help="Do not upload the folder's other files as attachments.",
        ),
    ] = False,
) -> None:
    """Submit one folder: map, upload, finalize, wait for the checks, submit.

    A Tangra .lc or PyMovie .pymovie goes in the optional aperture_record slot.
    Files the five slots do not take (plots, notes) are uploaded as
    attachments; hidden files and subfolders never are. The attachment limits
    (25 files, 50 MB each, no executables or scripts) are checked before
    anything is created.

    Exits 4 when the draft came back with open findings or something missing,
    so the whole job is one command a script can branch on.

    Example: iota-hub submit ./20180305_9721_Doty_Observer_POS --draft
    """
    overrides: dict[str, Any] = {
        "report": report,
        "lightcurve": lightcurve,
        "log": log,
        "vizier": vizier,
        "aperture_record": aperture_record,
        "attachments": not no_attachments,
        "attach": tuple(attach or ()),
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


def _dry_run(directory: Path, overrides: dict[str, Any]) -> None:
    """The mapping, the sizes and the target, without touching the network."""
    # A dry run always says where a real run would go, production included.
    target = _settings(api_key=_UNUSED_KEY, always_announce=True).base_url
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
    attachments = [
        {"filename": path.name, "path": str(path), "size": path.stat().st_size}
        for path in mapping.attachments
    ]
    ignored = [path.name for path in mapping.ignored]
    if _OPTIONS.json:
        _emit(
            {
                "target": target,
                "directory": str(mapping.directory),
                "uploads": uploads,
                "attachments": attachments,
                "ignored": ignored,
            }
        )
        return
    if _term.styled():
        _out()
    _field("Directory", mapping.directory)
    _field("Outcome", "dry run, nothing sent", style="bold")
    files = [(slot, path) for slot, path in mapping.slots.items()]
    files += [("attachment", path) for path in mapping.attachments]
    _render_files([(slot, path.name, path.stat().st_size) for slot, path in files])
    if ignored:
        _out(f"Not uploaded (--no-attachments): {', '.join(ignored)}")


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
                "attachments": result.attachments,
                "ignored": ignored,
                "next_commands": next_actions_commands(observation),
                "skipped_checks": [
                    check.model_dump(mode="json") for check in result.skipped_checks
                ],
                "owc_commands": result.owc_commands,
                "web_url": observation.web_url,
                "observation": observation.model_dump(mode="json"),
            }
        )
        return

    if _term.styled():
        _out()
    _field("Observation", result.observation_id, style="bold")
    _field("Outcome", result.outcome, status=True)
    checks = observation.checks
    if checks is not None and result.outcome != "draft":
        noun = "finding" if checks.open_findings == 1 else "findings"
        summary = f"{checks.status}, {checks.open_findings} open {noun}"
        if checks.dismissed_findings:
            summary += f", {checks.dismissed_findings} dismissed"
        _field("Checks", summary)
    if result.outcome != "submitted" and observation.web_url:
        # The hand-off to the browser: finish the draft there if you prefer.
        _field("Web", observation.web_url)
    _render_identity(observation.metadata)
    sizes = {slot: file.size_bytes for slot, file in observation.files.items() if file}
    _render_files(
        [(slot, name, sizes.get(slot)) for slot, name in result.uploads.items()]
        + [("attachment", name, None) for name in result.attachments]
    )
    if ignored:
        _out(f"Not uploaded (--no-attachments): {', '.join(ignored)}")
    if result.outcome == "needs_attention":
        _render_readiness(observation, unless=result.outcome)
        if observation.checks is not None:
            _render_findings(observation.checks)
    _render_next(observation)


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------


@auth_app.command("login")
@handle_errors
def auth_login(yes: YesOption = False) -> None:
    """Read an API key and store it, with its base URL, in a profile.

    The profile is selected like every other command's: --profile, then
    IOTA_HUB_PROFILE, then default_profile, then "default". If it already holds
    a key, a terminal asks before replacing it (--yes does not ask); a piped
    key replaces it and says so on stderr.

    The key is read from a hidden prompt on a terminal, otherwise from one line
    on stdin -- never from a command-line argument, which would land in the
    shell history and in `ps`.

    Example: iota-hub auth login --profile dev --base-url https://host/dev/api
    """
    name = _auth_profile()
    stored = (config.load_config().get("profiles") or {}).get(name) or {}
    target = (
        _OPTIONS.base_url
        or os.environ.get(ENV_BASE_URL)
        or stored.get("base_url")
        or DEFAULT_BASE_URL
    ).rstrip("/")
    if target != DEFAULT_BASE_URL:
        _note(f"Target: {target}")

    replaced = _describe_stored_key(stored)
    if replaced and sys.stdin.isatty():
        _confirm(
            f"Profile '{name}' already holds {replaced}. "
            f"Replace it with a key for {target}?",
            yes,
        )

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
    if replaced:
        _note(f"Replaced profile '{name}', which held {replaced}.")

    prefix = config.key_prefix(key)
    if _OPTIONS.json:
        _emit(
            {
                "profile": name,
                "base_url": target,
                "key_prefix": prefix,
                "config_file": str(path),
                "replaced": bool(replaced),
            }
        )
        return
    _field("Profile", name)
    _field("Base URL", target)
    _field("Key", prefix)
    _field("Saved to", _pasteable(path))


def _auth_profile() -> str:
    """The profile ``auth login`` writes and ``auth logout`` deletes."""
    return config.select_profile(config.load_config(), _OPTIONS.profile)[0]


def _describe_stored_key(stored: dict[str, str]) -> str | None:
    """What a re-login would overwrite, safe to print; ``None`` when nothing."""
    if not stored.get("api_key"):
        return None
    prefix = config.key_prefix(stored["api_key"])
    return f"key {prefix} for {stored.get('base_url') or DEFAULT_BASE_URL}"


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
    _field("Config file", _pasteable(path))
    _field("Check", check, status=True)


@auth_app.command("logout")
@handle_errors
def auth_logout() -> None:
    """Forget one profile's stored key and base URL.

    The profile is selected like every other command's: --profile, then
    IOTA_HUB_PROFILE, then default_profile, then "default". Removing
    default_profile clears it: no other profile is chosen in its place.

    Example: iota-hub auth logout --profile dev
    """
    name = _auth_profile()
    before = config.load_config()
    deleted = config.delete_profile(name)
    path = config.config_file_path()
    if _OPTIONS.json:
        _emit({"profile": name, "deleted": deleted, "config_file": str(path)})
    elif deleted:
        _out(f"Removed profile '{name}' from {_pasteable(path)}")
    else:
        _out(f"No profile named '{name}' in {_pasteable(path)}")
    if deleted and before.get("default_profile") == name:
        remaining = {k: v for k, v in before["profiles"].items() if k != name}
        if remaining:
            _note_cleared_default(name, remaining)


def _note_cleared_default(name: str, remaining: dict[str, dict[str, str]]) -> None:
    """Say where bare commands go now that ``default_profile`` is gone."""
    _note(
        f"'{name}' was the default profile; no default is set now. "
        f"Remaining profiles: {', '.join(sorted(remaining))}."
    )
    if "default" in remaining:
        target = remaining["default"].get("base_url") or DEFAULT_BASE_URL
        _note(f"A command that names no profile now uses 'default' ({target}).")
    else:
        _note(
            "A command that names no profile now has no stored key; "
            "pass --profile or set IOTA_HUB_PROFILE."
        )


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
                _note(f"A check run is already under way for {observation_id}.")
        if no_wait:
            observation = client.get_observation(observation_id)
        else:
            observation = client.wait_for_checks(
                observation_id, timeout=timeout, on_progress=_progress
            )
            _warn_checks(observation)
    _emit_observation(observation)


@drafts_app.command("submit")
@handle_errors
def drafts_submit(observation_id: IdArgument) -> None:
    """Submit a draft that is ready. Exits 4, with the findings, when it is not.

    An observation that is already submitted is a success: it is printed as it
    is, exactly what the API's own submit would answer.

    Example: iota-hub drafts submit obs_01JABCDEF
    """
    settings = _settings()
    with _make_client(settings) as client:
        observation = client.get_observation(observation_id)
        if observation.submission_status == "submitted":
            # Asking for a state the observation is already in is success
            # (API spec 4.2): the read is the answer, no write needed.
            _emit_observation(observation)
            return
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
    the API's own refusal carries, so a script
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


@drafts_app.command("owc")
@handle_errors
def drafts_owc(
    observation_id: IdArgument,
    pick: Annotated[
        str | None,
        typer.Option(
            "--pick",
            metavar="OWC_EVENT_ID",
            help="Link one of the candidates `drafts show` lists.",
        ),
    ] = None,
    link: Annotated[
        str | None,
        typer.Option(
            "--link",
            metavar="URL_OR_ID",
            help="Link any OWC event; checked against OWC and the report.",
        ),
    ] = None,
    none: Annotated[
        bool, typer.Option("--none", help="Submit without an OWC link, deliberately.")
    ] = False,
) -> None:
    """Settle a draft's OWC link when the Hub could not choose one itself.

    The Hub matches the OWC event on its own after the report is uploaded;
    this is for the cases it leaves open (several candidates, a mismatch, not
    found). Exactly one of --pick, --link, --none.

    Example: iota-hub drafts owc obs_01JABCDEF --pick 1917-2448-97311-649282-U033649
    """
    chosen = [flag for flag, value in (("--pick", pick), ("--link", link)) if value]
    if none:
        chosen.append("--none")
    if len(chosen) != 1:
        raise typer.BadParameter(
            "give exactly one of --pick, --link or --none.", param_hint="--pick"
        )
    if pick:
        choice, event = "picked", pick
    elif link:
        choice, event = "pasted", link
    else:
        choice, event = "proceed_unlinked", None
    settings = _settings()
    with _make_client(settings) as client:
        current = client.get_observation(observation_id)
        observation = client.update_draft(
            observation_id,
            version=current.version,
            owc_link_choice=choice,
            owc_event=event,
        )
    _emit_observation(observation)


@drafts_app.command("confirm-asteroid")
@handle_errors
def drafts_confirm_asteroid(observation_id: IdArgument) -> None:
    """Confirm that a draft's non-numeric asteroid id is deliberate.

    A comet or an unnumbered designation is fine; the Hub only asks so that a
    numbered asteroid entered without its number is caught.

    Example: iota-hub drafts confirm-asteroid obs_01JABCDEF
    """
    settings = _settings()
    with _make_client(settings) as client:
        current = client.get_observation(observation_id)
        observation = client.update_draft(
            observation_id,
            version=current.version,
            confirm_unnumbered_asteroid_id=True,
        )
    _emit_observation(observation)


@drafts_app.command("comment")
@handle_errors
def drafts_comment(
    observation_id: IdArgument,
    text: Annotated[str, typer.Argument(metavar="TEXT")],
) -> None:
    """Set the free-text comment a reviewer reads on a draft.

    Example: iota-hub drafts comment obs_01JABCDEF "Clouds after R."
    """
    settings = _settings()
    with _make_client(settings) as client:
        current = client.get_observation(observation_id)
        observation = client.update_draft(
            observation_id, version=current.version, comments=text
        )
    _emit_observation(observation)


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
    """Put a new file in one slot of a draft, or add one attachment.

    A fixed slot is replaced; `attachment` adds one more (the limits are 25
    per observation, 50 MB each, no executables or scripts). On a submitted
    observation of yours this is an amendment: a replaced report must still
    describe the same observation (identity_changed, exit 2, otherwise), and
    once its event is in review the API refuses (already_processed, exit 1).

    Example: iota-hub drafts files add obs_01JABCDEF lightcurve ./fixed.csv
    Example: iota-hub drafts files add obs_01JABCDEF attachment ./plot.png
    """
    settings = _settings()
    with _make_client(settings) as client:
        if slot is SlotName.attachment:
            observation = client.add_attachment(
                observation_id, path, on_progress=_progress
            )
        else:
            observation = client.replace_file(
                observation_id, slot.value, path, on_progress=_progress
            )
    _emit_observation(observation)


@drafts_files_app.command("rm")
@handle_errors
def drafts_files_rm(
    observation_id: IdArgument,
    slot: SlotArgument,
    attachment_id: Annotated[
        str | None,
        typer.Argument(
            metavar="[ATTACHMENT_ID]",
            help="With SLOT attachment: which one (see `drafts show`).",
        ),
    ] = None,
    yes: YesOption = False,
) -> None:
    """Clear one file slot on a draft, or remove one attachment by its id.

    Example: iota-hub drafts files rm obs_01JABCDEF vizier --yes
    Example: iota-hub drafts files rm obs_01JABCDEF attachment 3f1c2b9e-... --yes
    """
    if slot is SlotName.attachment and not attachment_id:
        raise typer.BadParameter(
            "removing an attachment needs its id; `iota-hub drafts show ID` "
            "lists them.",
            param_hint="ATTACHMENT_ID",
        )
    if slot is not SlotName.attachment and attachment_id:
        raise typer.BadParameter(
            "only the attachment slot takes an id.", param_hint="ATTACHMENT_ID"
        )
    what = (
        f"attachment {attachment_id}"
        if slot is SlotName.attachment
        else f"the {slot.value} file"
    )
    _confirm(f"Remove {what} from {observation_id}?", yes)
    settings = _settings()
    with _make_client(settings) as client:
        if slot is SlotName.attachment:
            observation = client.delete_attachment(observation_id, str(attachment_id))
        else:
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
            _asteroid(item.metadata.asteroid_id, item.metadata.asteroid_name),
            _joined(item.metadata.star_catalog, item.metadata.star_id),
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


@observations_app.command("check")
@handle_errors
def observations_check(
    asteroid_name: Annotated[
        str, typer.Option("--asteroid-name", help="The asteroid's name.")
    ],
    star_id: Annotated[str, typer.Option("--star-id", help="The star's id.")],
    date: Annotated[
        str,
        typer.Option(
            "--date", metavar="YYYY-MM-DD", help="The UTC date of the observation."
        ),
    ],
    asteroid_id: Annotated[
        str | None,
        typer.Option(
            "--asteroid-id",
            help="The asteroid's number. Without it, no event lookup is made.",
        ),
    ] = None,
    star_catalog: Annotated[
        str | None,
        typer.Option("--star-catalog", help="The star's catalog, e.g. UCAC4, TYC."),
    ] = None,
    station: Annotated[
        str | None,
        typer.Option(
            "--station", help="The station number, for one of several stations."
        ),
    ] = None,
) -> None:
    """Do I already have this observation? Look it up by what it observed.

    Finds your own draft or submission for this asteroid, star, UTC date and
    station; otherwise says whether the event exists. Never another observer's
    observation. Run it before `submit` when an earlier run may have created
    the draft already. Exits 0 whatever it finds.

    Example: iota-hub observations check --asteroid-id 9721
    --asteroid-name Doty --star-id 1234-56789 --date 2018-03-05
    """
    settings = _settings()
    with _make_client(settings) as client:
        answer = client.check_observation(
            asteroid_id=asteroid_id,
            asteroid_name=asteroid_name,
            star_catalog=star_catalog,
            star_id=star_id,
            observed_date=date,
            station=station,
        )
    if _OPTIONS.json:
        _emit(answer.model_dump(mode="json"))
        return
    _render_check(answer)


_FOUND = {
    "observation": "yes, one of yours",
    "event": "no, but the event exists",
    "none": "no",
}


def _render_check(answer: PublicCheckResponse) -> None:
    _field("Found", _FOUND.get(answer.found, answer.found), style="bold")
    found = answer.observation
    if found is not None:
        _field("Observation", found.observation_id)
        _field("Status", found.status, status=True)
        if found.amendment_count:
            _field("Amended", f"{found.amendment_count} time(s)")
        _field("Web", found.web_url)
    if answer.event is not None:
        _field("Event", answer.event.event_id)
        _field("Event status", answer.event.status, status=True)
    if found is not None and found.status == "draft":
        _heading("Next")
        _command(f"iota-hub drafts show {found.observation_id}")


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
            _asteroid(item.asteroid_id, item.asteroid_name),
            _joined(item.star_catalog, item.star_id),
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
                "the five upload slots: damit, ground_track, attachment, ..."
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
    if not written:
        _diagnostic(_nothing_to_download(kind, resource_id, slot))
    for path in written:
        _out(str(path))


def _nothing_to_download(kind: ResourceKind, resource_id: str, slot: str | None) -> str:
    """The note that says an empty result was a result, not a silent failure."""
    note = f"No files to download for {kind.value} {resource_id}"
    note += f" in slot {slot}." if slot else "."
    if kind is ResourceKind.event:
        note += (
            " Without reviewer access, an event's file list is empty until the "
            "event is COMPLETE."
        )
    return note


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
