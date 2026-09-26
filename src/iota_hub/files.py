"""Folder → slot mapping: deterministic rules, never a guess.

One observation lives in one directory, named to the observer convention
(``YYYYMMDD_<number>_<name>_<lastname>_POS|NEG[-X]``). The rules below are
``features/public-api/design.md`` § 8.3 and ``docs/client-conventions.md`` § 11,
and nothing else: no content sniffing, no classification. When a rule does not
pick exactly one file, that is an error naming the candidates and the flag that
settles it — a wrong guess costs an observer a bad submission.

Every file the four slots do not take becomes an **attachment**, under the
server's attachment rules (``specs/public-api.md`` § 4.3), checked here before
anything is created so a folder the API would refuse fails before any call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .errors import IotaHubError

#: Every slot the public API accepts, in the order a client fills them.
SLOTS = ("report", "lightcurve", "log", "vizier")

#: The slots a draft cannot be submitted without.
REQUIRED_SLOTS = ("report", "lightcurve", "log")

#: The server's allow-list (``observation_file_validation.py``), slot by slot.
EXTENSIONS: dict[str, tuple[str, ...]] = {
    "report": (".xlsx", ".xls"),
    "lightcurve": (".csv",),
    "log": (".txt",),
    "vizier": (".dat",),
}

#: The CLI flag that overrides the rule for a slot, named in every error.
FLAGS = {slot: f"--{slot}" for slot in SLOTS}

#: The declared ``slot`` of an attachment. It repeats; the four above do not.
ATTACHMENT_SLOT = "attachment"

#: The server's attachment rules (``file_limits`` and
#: ``BLOCKED_ATTACHMENT_EXTENSIONS`` in the API). Checked client-side only to
#: fail fast: the server enforces the same three and has the last word.
MAX_ATTACHMENTS = 25
ATTACHMENT_MAX_BYTES = 50 * 1024 * 1024
BLOCKED_ATTACHMENT_EXTENSIONS = frozenset(
    {
        ".exe",
        ".bat",
        ".cmd",
        ".sh",
        ".ps1",
        ".js",
        ".py",
        ".php",
        ".pl",
        ".rb",
        ".jar",
        ".msi",
        ".dll",
        ".so",
    }
)


class MappingError(IotaHubError):
    """A folder the rules cannot map to exactly one observation.

    Codes: ``ambiguous_files`` (two candidates for one slot),
    ``missing_files`` (a required slot has none), ``invalid_extension`` (an
    explicit override has the wrong extension), ``not_a_directory``, and the
    attachment rules: ``blocked_attachment``, ``attachment_too_large`` and
    ``too_many_attachments``.
    """


@dataclass
class FolderMapping:
    """What a directory maps to: the slots filled, the attachments, and what
    was left alone (only with ``attachments=False``)."""

    directory: Path
    slots: dict[str, Path] = field(default_factory=dict)
    attachments: list[Path] = field(default_factory=list)
    ignored: list[Path] = field(default_factory=list)

    def missing_required(self) -> list[str]:
        """Required slots this mapping does not fill, in slot order."""
        return [slot for slot in REQUIRED_SLOTS if slot not in self.slots]


def map_folder(
    directory: str | Path,
    *,
    report: str | Path | None = None,
    lightcurve: str | Path | None = None,
    log: str | Path | None = None,
    vizier: str | Path | None = None,
    attachments: bool = True,
    attach: list[str | Path] | tuple[str | Path, ...] = (),
) -> FolderMapping:
    """Map one directory to slots and attachments, or raise :class:`MappingError`.

    | Slot | Rule |
    |---|---|
    | ``report`` | exactly one ``.xlsx``/``.xls`` |
    | ``lightcurve`` | exactly one ``.csv`` |
    | ``log`` | exactly one ``.txt`` whose name contains ``log`` |
    | ``vizier`` | at most one ``.dat`` |

    The listing is non-recursive and skips dotfiles and subdirectories;
    extensions are matched case-insensitively. An explicit path replaces the
    rule for its slot and is checked only for its extension — the ``log`` name
    rule exists to tell two ``.txt`` files apart, and a caller who names the
    file has already done that.

    Every other file in the listing is an attachment; ``attachments=False``
    leaves them out and reports them on ``ignored`` instead. ``attach`` adds
    files from anywhere (honoured either way). The attachment rules are then
    checked for all of them at once (:func:`check_attachments`).
    """
    base = Path(directory)
    if not base.is_dir():
        raise MappingError(
            "not_a_directory",
            f"{base} is not a directory.",
            hint="Pass the folder holding one observation's files.",
            details={"path": str(base)},
        )

    overrides = _overrides(report=report, lightcurve=lightcurve, log=log, vizier=vizier)
    entries = sorted(
        entry
        for entry in base.iterdir()
        if entry.is_file() and not entry.name.startswith(".")
    )

    slots: dict[str, Path] = {}
    # Resolved, so an override spelled ``./dir/lc.csv`` still matches the
    # ``dir/lc.csv`` the listing produced and is not reported as ignored.
    claimed: set[Path] = set()
    for slot in SLOTS:
        if slot in overrides:
            slots[slot] = overrides[slot]
            claimed.add(overrides[slot].resolve())
            continue
        candidates = [entry for entry in entries if _matches(slot, entry)]
        if len(candidates) > 1:
            raise _ambiguous(slot, candidates)
        if candidates:
            slots[slot] = candidates[0]
            claimed.add(candidates[0].resolve())

    leftovers = [entry for entry in entries if entry.resolve() not in claimed]
    mapping = FolderMapping(
        directory=base,
        slots=slots,
        attachments=leftovers if attachments else [],
        ignored=[] if attachments else leftovers,
    )
    missing = mapping.missing_required()
    if missing:
        raise _missing(base, missing)

    seen = {path.resolve() for path in mapping.attachments}
    for value in attach:
        path = Path(value)
        if not path.is_file():
            raise MappingError(
                "missing_files",
                f"No such file to attach: {path}",
                hint="Check the path passed to --attach.",
                details={"path": str(path)},
            )
        if path.resolve() in seen or path.resolve() in claimed:
            continue
        seen.add(path.resolve())
        mapping.attachments.append(path)
    check_attachments(mapping.attachments)
    return mapping


def check_attachments(paths: list[Path], *, already_attached: int = 0) -> None:
    """Apply the server's attachment rules to ``paths``, naming every offender.

    Blocked extensions first (trailing dots and spaces stripped, as the server
    does), then the per-file size, then the count. Each error lists all the
    files it applies to, so one run tells the observer everything to fix.
    """
    blocked = [path for path in paths if _blocked_extension(path.name)]
    if blocked:
        names = ", ".join(path.name for path in blocked)
        raise MappingError(
            "blocked_attachment",
            f"The API refuses these file types as attachments: {names}.",
            hint=(
                "Move them out of the folder, or pass --no-attachments to upload "
                "only the report, light curve, log and VizieR files."
            ),
            details={
                "files": [path.name for path in blocked],
                "blocked_extensions": sorted(BLOCKED_ATTACHMENT_EXTENSIONS),
            },
        )
    too_large = [path for path in paths if path.stat().st_size > ATTACHMENT_MAX_BYTES]
    if too_large:
        names = ", ".join(
            f"{path.name} ({path.stat().st_size} bytes)" for path in too_large
        )
        raise MappingError(
            "attachment_too_large",
            f"Attachments may be at most {ATTACHMENT_MAX_BYTES} bytes (50 MB): "
            f"{names}.",
            hint="Move them out of the folder, or pass --no-attachments.",
            details={
                "files": [path.name for path in too_large],
                "max_size_bytes": ATTACHMENT_MAX_BYTES,
            },
        )
    total = already_attached + len(paths)
    if total > MAX_ATTACHMENTS:
        raise MappingError(
            "too_many_attachments",
            f"{total} attachments; an observation takes at most "
            f"{MAX_ATTACHMENTS}. Attachments: "
            f"{', '.join(path.name for path in paths)}.",
            hint="Move some out of the folder, or pass --no-attachments.",
            details={
                "files": [path.name for path in paths],
                "count": total,
                "max_allowed": MAX_ATTACHMENTS,
            },
        )


def _blocked_extension(filename: str) -> str | None:
    """The blocked extension ``filename`` ends in, if any (the server's rule)."""
    name = filename.lower().rstrip(". ")
    return next(
        (ext for ext in BLOCKED_ATTACHMENT_EXTENSIONS if name.endswith(ext)), None
    )


# -- rules ------------------------------------------------------------------


def _matches(slot: str, path: Path) -> bool:
    if path.suffix.lower() not in EXTENSIONS[slot]:
        return False
    if slot == "log":
        return "log" in path.name.lower()
    return True


def _overrides(**paths: str | Path | None) -> dict[str, Path]:
    """The explicit paths, checked for existence and extension."""
    resolved: dict[str, Path] = {}
    for slot, value in paths.items():
        if value is None:
            continue
        path = Path(value)
        if not path.is_file():
            raise MappingError(
                "missing_files",
                f"No such file for the {slot} slot: {path}",
                hint=f"Check the path passed to {FLAGS[slot]}.",
                details={"missing": [slot], "path": str(path)},
            )
        allowed = EXTENSIONS[slot]
        if path.suffix.lower() not in allowed:
            raise MappingError(
                "invalid_extension",
                f"{path.name} is not a {slot} file: the API accepts "
                f"{', '.join(allowed)} in that slot.",
                hint=f"Pass a {allowed[0]} file to {FLAGS[slot]}.",
                details={
                    "slot": slot,
                    "filename": path.name,
                    "allowed_extensions": list(allowed),
                },
            )
        resolved[slot] = path
    return resolved


# -- errors -----------------------------------------------------------------


def _ambiguous(slot: str, candidates: list[Path]) -> MappingError:
    names = ", ".join(path.name for path in candidates)
    hint = f"Pass the one you mean: {FLAGS[slot]} PATH."
    if slot == "report":
        hint = (
            f"{hint} A folder holding several stations (_POS-1, _POS-2) is not "
            "supported yet: submit one observation per folder."
        )
    return MappingError(
        "ambiguous_files",
        f"{len(candidates)} files could be the {slot}: {names}.",
        hint=hint,
        details={"slot": slot, "candidates": [path.name for path in candidates]},
    )


def _missing(directory: Path, missing: list[str]) -> MappingError:
    rules = {
        "report": "one .xlsx or .xls file",
        "lightcurve": "one .csv file",
        "log": "one .txt file with 'log' in its name",
    }
    wanted = "; ".join(f"{slot}: {rules[slot]}" for slot in missing)
    flags = " ".join(f"{FLAGS[slot]} PATH" for slot in missing)
    return MappingError(
        "missing_files",
        f"{directory} has no {' or '.join(missing)} file ({wanted}).",
        hint=f"Add the file to the folder, or name it explicitly: {flags}.",
        details={"missing": missing, "directory": str(directory)},
    )
