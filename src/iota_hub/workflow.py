"""The workflow layer: the part the API cannot do for you.

Mixed into :class:`~iota_hub.client.Client`, so a caller sees one object. Every
decision here — what to declare, when to re-initialize an upload, when a check
run is over, when it is safe to submit — is written down language-neutrally in
``docs/client-conventions.md``; this module is the Python rendering of it, and
the two change together.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ._http import sha256_base64
from .errors import IotaHubError
from .files import SLOTS, FolderMapping, map_folder
from .models import (
    PublicDeclaredFile,
    PublicEventFileLink,
    PublicFileLink,
    PublicFinding,
    PublicObservation,
    PublicUploadTarget,
)

#: Polling policy (conventions § 8): 2 s, then x1.5, capped at 10 s.
POLL_FIRST_INTERVAL = 2.0
POLL_GROWTH = 1.5
POLL_CAP = 10.0

#: Default wait for a check run, in seconds.
POLL_TIMEOUT = 600.0

#: A run is over unless it is one of these. Testing the *non*-terminal values
#: means a status the API adds later ends the wait instead of hanging forever.
IN_FLIGHT = frozenset({"running", "pending"})

Progress = Callable[[str], None]


@dataclass
class SubmitResult:
    """What one ``submit_files`` / ``submit_folder`` call ended up doing.

    ``outcome`` is the vocabulary a script branches on (conventions § 12):

    | value | meaning |
    |---|---|
    | ``submitted`` | the observation is submitted |
    | ``ready`` | clean, but ``submit=False`` left it a draft |
    | ``needs_attention`` | findings, missing items, or the run errored |
    | ``draft`` | ``wait=False``: uploaded and finalized, not waited on |
    """

    observation: PublicObservation
    submitted: bool
    outcome: str
    uploads: dict[str, str] = field(default_factory=dict)
    ignored: list[Path] = field(default_factory=list)

    @property
    def observation_id(self) -> str:
        return self.observation.observation_id


class WorkflowMixin:
    """``submit_files``, ``wait_for_checks``, ``replace_file``, downloads."""

    # -- submitting --------------------------------------------------------

    def submit_files(
        self,
        files: Mapping[str, str | Path],
        *,
        auto_submit_when_clean: bool = False,
        wait: bool = True,
        submit: bool = True,
        timeout: float = POLL_TIMEOUT,
        on_progress: Progress | None = None,
    ) -> SubmitResult:
        """Create a draft from ``{slot: path}``, upload, finalize, submit.

        The whole job in one call: declare every file with its size and base64
        SHA-256, POST each presigned target, finalize, wait for the observer
        checks and submit when — and only when — the draft polls back
        ``ready``. A draft that is not ready is left alone for the caller to
        fix (the findings are on the returned observation); it is never
        deleted, because a partial draft is recoverable in the web app.
        """
        paths = _paths(files)
        declared = [
            PublicDeclaredFile(
                slot=slot,
                filename=path.name,
                size=path.stat().st_size,
                sha256=sha256_base64(path),
            )
            for slot, path in paths.items()
        ]
        uploads = {slot: path.name for slot, path in paths.items()}

        _say(on_progress, f"Creating draft ({len(declared)} files)...")
        created = self.create_draft(
            declared,
            auto_submit_when_clean=auto_submit_when_clean,
            idempotency_key=_idempotency_key(),
        )
        observation_id = created.observation.observation_id

        total = len(created.uploads)
        for index, target in enumerate(created.uploads, start=1):
            _say(on_progress, f"Uploading {target.slot} ({index}/{total})...")
            self._upload(
                observation_id, target, paths[target.slot], on_progress=on_progress
            )

        _say(on_progress, "Finalizing draft...")
        observation = self.finalize_draft(
            observation_id, idempotency_key=_idempotency_key()
        )
        if not wait:
            return SubmitResult(observation, False, "draft", uploads)

        observation = self.wait_for_checks(
            observation_id, timeout=timeout, on_progress=on_progress
        )
        # ``auto_submit_when_clean`` means the validation worker may have
        # submitted it while we polled; that is a success, not an oversight.
        if observation.submission_status == "submitted":
            return SubmitResult(observation, True, "submitted", uploads)
        if not _is_ready(observation):
            return SubmitResult(observation, False, "needs_attention", uploads)
        if not submit:
            return SubmitResult(observation, False, "ready", uploads)

        _say(on_progress, "Submitting...")
        observation = self.submit_draft(
            observation_id,
            version=observation.version,
            idempotency_key=_idempotency_key(),
        )
        return SubmitResult(observation, True, "submitted", uploads)

    def submit_folder(
        self,
        directory: str | Path,
        *,
        report: str | Path | None = None,
        lightcurve: str | Path | None = None,
        log: str | Path | None = None,
        vizier: str | Path | None = None,
        **kwargs: object,
    ) -> SubmitResult:
        """Map one directory to slots (``iota_hub.files``) and submit it.

        The files the mapping did not use come back on
        :attr:`SubmitResult.ignored` — they are not uploaded, and the public
        surface has no attachment slot to put them in.
        """
        mapping: FolderMapping = map_folder(
            directory,
            report=report,
            lightcurve=lightcurve,
            log=log,
            vizier=vizier,
        )
        result = self.submit_files(mapping.slots, **kwargs)  # type: ignore[arg-type]
        result.ignored = mapping.ignored
        return result

    # -- polling -----------------------------------------------------------

    def wait_for_checks(
        self,
        observation_id: str,
        *,
        timeout: float = POLL_TIMEOUT,
        on_progress: Progress | None = None,
    ) -> PublicObservation:
        """Poll the draft until its observer check run is over.

        ``timeout`` bounds the time spent *waiting between* polls — the
        requests themselves are bounded by the transport's own timeout — so
        the wait is deterministic with an injected ``sleep`` and tests never
        sleep at all.
        """
        waited = 0.0
        interval = POLL_FIRST_INTERVAL
        while True:
            observation = self.get_observation(observation_id)
            if _checks_done(observation):
                return observation
            if waited >= timeout:
                raise IotaHubError(
                    "timeout",
                    f"The checks on {observation_id} were still running after "
                    f"{timeout:g}s.",
                    hint=(
                        "The run is still going; poll again with "
                        f"`iota-hub drafts show {observation_id}`."
                    ),
                    details={
                        "observation_id": observation_id,
                        "timeout": timeout,
                        "checks_status": (
                            observation.checks.status if observation.checks else None
                        ),
                    },
                )
            delay = min(interval, timeout - waited)
            _say(on_progress, "Waiting for checks...")
            self.http.sleep(delay)
            waited += delay
            interval = min(interval * POLL_GROWTH, POLL_CAP)

    # -- the fix loop ------------------------------------------------------

    def replace_file(
        self,
        observation_id: str,
        slot: str,
        path: str | Path,
        *,
        on_progress: Progress | None = None,
    ) -> PublicObservation:
        """Put a new file in one slot: init, upload, finalize."""
        file = Path(path)
        _say(on_progress, f"Uploading {slot} ({file.name})...")
        target = self.init_file_upload(
            observation_id, slot, filename=file.name, sha256=sha256_base64(file)
        )
        self.http.upload_to_s3(target, file)
        return self.finalize_file_upload(
            observation_id,
            slot,
            file_key=target.file_key,
            filename=file.name,
            idempotency_key=_idempotency_key(),
        )

    # -- downloads ---------------------------------------------------------

    def download_files(
        self,
        kind: str,
        id: str,
        dest: str | Path,
        *,
        slot: str | None = None,
        overwrite: bool = False,
        on_progress: Progress | None = None,
    ) -> list[Path]:
        """Download an observation's or an event's files into ``dest``.

        The links are presigned, so they are fetched **without** the API key.
        Nothing is skipped silently: an existing file is ``file_exists`` unless
        ``overwrite`` is set, and a link S3 refuses (an expired one answers
        ``403``) is ``download_failed``.
        """
        if kind == "observation":
            links = self.list_observation_files(id).files
        elif kind == "event":
            links = self.list_event_files(id).files
        else:
            raise ValueError(f"kind must be 'observation' or 'event', not {kind!r}")

        if slot is not None:
            links = [link for link in links if link.slot == slot]

        directory = Path(dest)
        directory.mkdir(parents=True, exist_ok=True)
        # Check every destination before writing any of it: a refusal halfway
        # through would leave the caller with a partial download.
        targets = [(link, directory / _safe_name(link)) for link in links]
        if not overwrite:
            for link, out in targets:
                if out.exists():
                    raise IotaHubError(
                        "file_exists",
                        f"{out} already exists.",
                        hint=(
                            "Download into another directory, or "
                            "overwrite it (--force)."
                        ),
                        details={"path": str(out), "slot": link.slot},
                    )

        written: list[Path] = []
        for link, out in targets:
            _say(on_progress, f"Downloading {out.name}...")
            self._download(link.url, out, slot=link.slot)
            written.append(out)
        return written

    # -- internals ---------------------------------------------------------

    def _upload(
        self,
        observation_id: str,
        target: PublicUploadTarget,
        path: Path,
        *,
        on_progress: Progress | None = None,
    ) -> None:
        """POST one file, re-initializing its target once if that fails.

        A presigned target expires, so a failed upload is retried with a fresh
        one rather than blindly (conventions § 6). The fresh target belongs to
        the per-slot fix loop, so it is committed with the per-slot finalize:
        that is what drops the slot from the draft's ``pending_uploads``, and
        without it the collapsed finalize would keep answering
        ``409 upload_missing`` for the target that expired.
        """
        try:
            self.http.upload_to_s3(target, path)
            return
        except IotaHubError:
            _say(on_progress, f"Retrying {target.slot} with a fresh upload target...")

        fresh = self.init_file_upload(
            observation_id, target.slot, filename=path.name, sha256=sha256_base64(path)
        )
        self.http.upload_to_s3(fresh, path)
        self.finalize_file_upload(
            observation_id,
            target.slot,
            file_key=fresh.file_key,
            filename=path.name,
            idempotency_key=_idempotency_key(),
        )

    def _download(self, url: str, out: Path, *, slot: str) -> None:
        # ``self.http.client`` sends no API key of its own (the key is a
        # per-request header on API calls), which is what a presigned URL wants.
        with self.http.client.stream("GET", url) as response:
            if not response.is_success:
                response.read()
                raise IotaHubError(
                    "download_failed",
                    f"The download link for {out.name} was refused with HTTP "
                    f"{response.status_code}.",
                    hint="Links expire; list the files again for fresh ones.",
                    details={"slot": slot, "filename": out.name},
                    status=response.status_code,
                )
            with open(out, "wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)


# --------------------------------------------------------------------------
# next_actions → commands
# --------------------------------------------------------------------------


def next_actions_commands(observation: PublicObservation) -> list[str]:
    """The literal ``iota-hub`` commands the API's ``next_actions`` ask for.

    Lives in the library, not the CLI, so a script that embeds the library
    prints the same guidance the CLI does. An unknown verb is never dropped:
    it becomes a pointer at ``iota-hub guide`` (the verbs are additive, and a
    client must not pretend a verb it does not know is not there).
    """
    observation_id = observation.observation_id
    commands: list[str] = []
    for verb in observation.next_actions:
        for command in _commands_for(verb, observation_id, observation):
            if command not in commands:
                commands.append(command)
    return commands


def _commands_for(
    verb: str, observation_id: str, observation: PublicObservation
) -> list[str]:
    if verb.startswith("upload:"):
        slot = verb.split(":", 1)[1]
        return [f"iota-hub drafts files add {observation_id} {slot} <path>"]
    if verb == "run_checks":
        return [f"iota-hub drafts check {observation_id}"]
    if verb == "poll":
        return [f"iota-hub drafts show {observation_id}"]
    if verb == "submit":
        return [f"iota-hub drafts submit {observation_id}"]
    if verb == "dismiss_or_fix":
        commands = [
            f"iota-hub drafts dismiss {observation_id} {finding.fingerprint} "
            f'--note "why this is expected"'
            for finding in _open_findings(observation)
        ]
        commands.append(
            f"iota-hub drafts files add {observation_id} <slot> <path>"
            "   (fix the file instead of dismissing)"
        )
        return commands
    if verb == "confirm_asteroid_id":
        return [
            "Confirm the asteroid id in the web app: the public API has no verb for it."
        ]
    if verb == "resolve_event_files_conflict":
        return [
            "Resolve the staged event-files conflict in the web app: the "
            "public API has no verb for it."
        ]
    return [f"{verb}: see iota-hub guide"]


def _open_findings(observation: PublicObservation) -> list[PublicFinding]:
    if observation.checks is None:
        return []
    return [f for f in observation.checks.findings if f.dismissal is None]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _safe_name(link: PublicFileLink | PublicEventFileLink) -> str:
    """The file's bare name, refusing one that does not name a file.

    ``filename`` comes from the server: everything but the last component is
    dropped, and a name that leaves nothing to write to -- empty, ``.`` or
    ``..`` -- is refused before any byte is written.
    """
    name = Path(link.filename).name
    if name in {"", ".", ".."}:
        raise IotaHubError(
            "download_failed",
            f"The API named a file this client will not write: {link.filename!r}.",
            hint="Download it from the web app, and report the filename.",
            details={"filename": link.filename, "slot": link.slot},
        )
    return name


def _paths(files: Mapping[str, str | Path]) -> dict[str, Path]:
    """``{slot: path}`` in slot order, with the slot names checked."""
    unknown = sorted(set(files) - set(SLOTS))
    if unknown:
        raise ValueError(
            f"unknown slot(s) {', '.join(unknown)}; expected {', '.join(SLOTS)}"
        )
    return {slot: Path(files[slot]) for slot in SLOTS if slot in files}


def _is_ready(observation: PublicObservation) -> bool:
    return observation.readiness is not None and observation.readiness.state == "ready"


def _checks_done(observation: PublicObservation) -> bool:
    """Terminal: the run finished, or there is nothing left to poll for."""
    if observation.submission_status == "submitted":
        return True
    checks = observation.checks
    if checks is None:
        # No run to wait for. A draft still missing a file says so with its
        # upload verbs and never emits ``poll``; waiting on it would hang.
        return "poll" not in observation.next_actions
    return checks.status not in IN_FLIGHT


def _idempotency_key() -> str:
    """One uuid4 per logical call; every transport retry of it reuses this."""
    return str(uuid.uuid4())


def _say(on_progress: Progress | None, message: str) -> None:
    if on_progress is not None:
        on_progress(message)
