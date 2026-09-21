"""Pydantic models mirroring the ``Public*`` schemas of ``spec/openapi.json``.

Field names and required-ness match the schemas exactly. Two rules make these
models survive the API's additive-only evolution (specs/public-api.md § 3):

* ``extra="ignore"`` — a field added to the API never breaks an installed
  client.
* **Open string enums.** Where the API documents a set of values that may grow
  (``readiness.state``, ``next_actions`` verbs, ``checks.status``,
  ``submission_status``, ``severity``, …) the type here is plain ``str``. A new
  verb must not make the response unparseable. Only the closed sets a *request*
  must pick from — the file slots — are ``Literal``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: The file slots a client may fill. Closed: the server rejects anything else.
Slot = Literal["report", "lightcurve", "log", "vizier"]


class _Public(BaseModel):
    model_config = ConfigDict(extra="ignore")


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


class PublicDeclaredFile(_Public):
    """One file declared up front when creating a draft."""

    slot: Slot
    filename: str
    size: int
    sha256: str | None = None


class PublicDraftCreateRequest(_Public):
    """Body of ``POST /observations/drafts``."""

    files: list[PublicDeclaredFile] = Field(default_factory=list)
    auto_submit_when_clean: bool = False


class PublicInitUploadRequest(_Public):
    """Body of ``POST …/files/{slot}/upload/init``."""

    filename: str
    sha256: str | None = None


class PublicFinalizeUploadRequest(_Public):
    """Body of ``POST …/files/{slot}/upload/finalize``."""

    file_key: str
    filename: str


class PublicDismissRequest(_Public):
    """Body of ``POST …/validation/findings/{fingerprint}/dismiss``."""

    note: str


class PublicSubmitRequest(_Public):
    """Body of ``POST /observations/{id}/submit``."""

    version: int


# --------------------------------------------------------------------------
# Shared pieces
# --------------------------------------------------------------------------


class PublicError(_Public):
    """The problem-details body. See :mod:`iota_hub.errors` for the exceptions."""

    code: str
    message: str
    hint: str | None = None
    details: dict[str, Any] | None = None


class PublicFileVersion(_Public):
    """The current file in one slot."""

    slot: str
    filename: str
    size_bytes: int
    uploaded_at: str
    content_type: str | None = None


class PublicUploadTarget(_Public):
    """A presigned POST target: post ``fields`` as given, the file last."""

    slot: str
    filename: str
    upload_url: str
    fields: dict[str, str] = Field(default_factory=dict)
    file_key: str


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


class PublicEvidence(_Public):
    """One evidence row backing a finding."""

    source: str
    field: str
    expected: str
    actual: str
    expected_label: str = "Expected"
    actual_label: str = "Actual"
    mismatch: bool = True
    file_ref: str | None = None


class PublicDismissal(_Public):
    """A dismissal recorded against a finding. ``via`` is ``"web"`` or ``"api"``."""

    dismissed_by: str
    dismissed_at: str
    note: str
    via: str | None = None


class PublicFinding(_Public):
    """One issue an observer check surfaced on a draft."""

    check_number: int
    code: str
    name: str
    severity: str
    message: str
    how_to_fix: str | None = None
    doc_url: str | None = None
    fingerprint: str
    evidence: list[PublicEvidence] = Field(default_factory=list)
    dismissal: PublicDismissal | None = None


class PublicChecks(_Public):
    """The latest observer-check run on a draft."""

    run_id: str
    status: str
    checks_current: bool = False
    open_findings: int = 0
    dismissed_findings: int = 0
    findings: list[PublicFinding] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Observations
# --------------------------------------------------------------------------


class PublicReadiness(_Public):
    """Derived submit readiness of a draft (never stored)."""

    state: str
    missing_required: list[str] = Field(default_factory=list)
    open_findings: int = 0
    dismissed_findings: int = 0
    checks_current: bool = False
    event_files_blocked: bool = False
    latest_run_id: str | None = None


class PublicObservationMetadata(_Public):
    """Identity and result fields. The first eight are report-derived."""

    observer_name: str | None = None
    observer_email: str | None = None
    asteroid_id: str | None = None
    asteroid_name: str | None = None
    star_catalog: str | None = None
    star_id: str | None = None
    observed_at_utc: str | None = None
    result_type: str | None = None
    comments: str | None = None
    confirm_unnumbered_asteroid_id: bool = False
    bypass_required_files: bool = False


class PublicReportParse(_Public):
    """Outcome of the server-side parse of the ``report`` slot."""

    status: str
    code: str | None = None
    message: str | None = None


class PublicObservation(_Public):
    """An observation (draft or submitted), self-describing for a poller."""

    observation_id: str
    submission_status: str
    submitted_at: str | None = None
    created_at: str
    updated_at: str
    version: int
    created_via: str = "web"
    event_id: str | None = None
    observer_user_id: str
    metadata: PublicObservationMetadata
    files: dict[str, PublicFileVersion | None] = Field(default_factory=dict)
    readiness: PublicReadiness | None = None
    checks: PublicChecks | None = None
    next_actions: list[str] = Field(default_factory=list)
    report_parse: PublicReportParse | None = None


class PublicObservationList(_Public):
    """A cursor-paginated page of observations."""

    items: list[PublicObservation] = Field(default_factory=list)
    next_cursor: str | None = None


class PublicDraftCreated(_Public):
    """Response to ``POST /observations/drafts``."""

    observation: PublicObservation
    uploads: list[PublicUploadTarget] = Field(default_factory=list)


class PublicFileLink(_Public):
    """A time-limited download link for one observation file."""

    slot: str
    filename: str
    size_bytes: int
    url: str
    expires_in: int


class PublicFileList(_Public):
    """``GET /observations/{id}/files``."""

    observation_id: str
    files: list[PublicFileLink] = Field(default_factory=list)
    zip_url: str | None = None
    expires_in: int


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


class PublicEvent(_Public):
    """An event as the public API returns it."""

    event_id: str
    status: str
    asteroid_id: str
    asteroid_name: str
    star_catalog: str
    star_id: str
    date_window_utc: str
    observation_ids: list[str] = Field(default_factory=list)
    observer_names: list[str] = Field(default_factory=list)
    assigned_reviewer_ids: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    public_notes: str | None = None
    files: dict[str, PublicFileVersion | None] | None = None


class PublicEventList(_Public):
    """A cursor-paginated page of events."""

    items: list[PublicEvent] = Field(default_factory=list)
    next_cursor: str | None = None


class PublicEventFileLink(_Public):
    """A time-limited download link for one event file."""

    slot: str
    filename: str
    size_bytes: int
    url: str
    expires_in: int


class PublicEventFileList(_Public):
    """``GET /events/{id}/files``."""

    event_id: str
    files: list[PublicEventFileLink] = Field(default_factory=list)
    expires_in: int
