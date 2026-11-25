"""Pydantic models mirroring the ``Public*`` schemas of ``spec/openapi.json``.

Field names and required-ness match the schemas exactly. Two rules make these
models survive the API's additive-only evolution:

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
Slot = Literal["report", "lightcurve", "log", "vizier", "aperture_record"]

#: What a draft-create may declare: a fixed slot, or an attachment (repeatable).
DeclaredSlot = Literal[
    "report", "lightcurve", "log", "vizier", "aperture_record", "attachment"
]


class _Public(BaseModel):
    model_config = ConfigDict(extra="ignore")


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


class PublicDeclaredFile(_Public):
    """One file declared up front when creating a draft."""

    slot: DeclaredSlot
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


class PublicFinalizeAttachmentRequest(_Public):
    """Body of ``POST …/attachments/upload/finalize``."""

    file_key: str
    attachment_id: str
    filename: str


class PublicDismissRequest(_Public):
    """Body of ``POST …/validation/findings/{fingerprint}/dismiss``."""

    note: str


class PublicSubmitRequest(_Public):
    """Body of ``POST /observations/{id}/submit``.

    ``version`` is optional: omitted, the current version is submitted;
    given, a mismatch is ``409 stale_version``.
    """

    version: int | None = None


class PublicCheckRequest(_Public):
    """Body of ``POST /observations/check``: an observation's natural identity.

    ``asteroid_id`` and ``star_catalog`` are compared only when given, and
    without ``asteroid_id`` there is no event lookup. ``station`` absent, null
    and ``""`` are the same station.
    """

    asteroid_id: str | None = None
    asteroid_name: str
    star_catalog: str | None = None
    star_id: str
    observed_date: str
    station: str | None = None


#: What ``PATCH /observations/{id}`` may set the OWC link to. Closed: the
#: server refuses anything else (``matched`` is the server's own).
OWCChoice = Literal["picked", "pasted", "proceed_unlinked"]


class PublicDraftUpdateRequest(_Public):
    """Body of ``PATCH /observations/{id}`` -- the client-owned fields.

    Sent with ``exclude_unset``: a field not given is left unchanged.
    """

    version: int
    owc_link_choice: OWCChoice | None = None
    owc_event: str | None = None
    confirm_unnumbered_asteroid_id: bool | None = None
    comments: str | None = None
    bypass_required_files: bool | None = None


class PublicKeyExchangeRequest(_Public):
    """Optional body of ``POST /key-exchange`` and of the sign-in start.

    ``client`` names the application; omitted, the Hub assumes ``tangra``.
    """

    client: str | None = None


class PublicCollectRequest(_Public):
    """Body of ``POST /key-exchange/sign-in/{request_id}/collect``."""

    collect_code: str = Field(repr=False)


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
    attachment_id: str | None = None


class PublicAttachment(_Public):
    """One optional attachment on an observation, removable by its id."""

    attachment_id: str
    slot: str = "attachment"
    filename: str
    size_bytes: int
    uploaded_at: str


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


class PublicSkippedCheck(_Public):
    """A check that did not run on this draft, and why. Never blocks submit."""

    check_number: int
    code: str
    name: str
    status: str
    reason: str


class PublicChecks(_Public):
    """The latest observer-check run on a draft."""

    run_id: str
    status: str
    checks_current: bool = False
    open_findings: int = 0
    dismissed_findings: int = 0
    findings: list[PublicFinding] = Field(default_factory=list)
    skipped: list[PublicSkippedCheck] = Field(default_factory=list)


# --------------------------------------------------------------------------
# The OWC link
# --------------------------------------------------------------------------


class PublicOWCCandidate(_Public):
    """One Occult Watcher Cloud event the report could belong to."""

    owc_event_id: str
    owc_event_url: str
    asteroid: str | None = None
    star: str | None = None
    star_mag: float | None = None
    closest_approach_utc: str | None = None
    mag_drop: float | None = None


class PublicOWCMismatch(_Public):
    """One clear disagreement between the report and an OWC event."""

    field: str
    report_value: str
    owc_value: str


class PublicOWCResolution(_Public):
    """What the server's own OWC match found for the report.

    ``status``: ``matched``, ``not_found``, ``ambiguous``, ``mismatch``,
    ``error`` or ``skipped`` -- an open set, like every status here.
    """

    status: str
    reason: str | None = None
    owc_event_id: str | None = None
    candidates: list[PublicOWCCandidate] = Field(default_factory=list)
    mismatches: list[PublicOWCMismatch] = Field(default_factory=list)
    resolved_at: str | None = None
    report_check: str | None = None
    report_owc_event_id: str | None = None


class PublicOWC(_Public):
    """The observation's link to an OWC event, and on a draft the server's match."""

    choice: str | None = None
    owc_event_id: str | None = None
    owc_event_url: str | None = None
    resolution: PublicOWCResolution | None = None


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
    submit_in_progress: bool = False


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
    station: str | None = None
    comments: str | None = None
    confirm_unnumbered_asteroid_id: bool = False
    bypass_required_files: bool = False


class PublicReportParse(_Public):
    """Outcome of the server-side parse of the ``report`` slot."""

    status: str
    code: str | None = None
    message: str | None = None


class PublicReportEvent(_Public):
    """One disappearance/reappearance pair of a Tangra JSON report.

    ``report_details.events[0]`` is the same event as the top-level
    ``d_corrected`` / ``r_corrected`` / ``d_err`` / ``r_err``.
    """

    d_corrected: str | None = None
    r_corrected: str | None = None
    d_err: float | None = None
    r_err: float | None = None
    snr: float | None = None
    second_star: bool = False


class PublicReportStarB(_Public):
    """Tangra's test of whether the second star of a double star is real."""

    verdict: str | None = None
    doubtful: bool | None = None
    p_value: float | None = None
    p_value_is_bound: bool | None = None
    trials: int | None = None
    step_elsewhere: bool | None = None
    d_elsewhere: float | None = None
    r_elsewhere: float | None = None
    d_spread_points: float | None = None
    r_spread_points: float | None = None
    slow_edge_fits_as_well: bool | None = None
    full_disappearance_p_value: float | None = None
    lifted_by_full_disappearance: bool | None = None


class PublicReportDoubleStar(_Public):
    """One double star of a Tangra JSON report: its two events and their light.

    ``event_a`` and ``event_b`` are 1-based indexes into ``events``.
    """

    event_a: int | None = None
    event_b: int | None = None
    arrangement: str | None = None
    edges: str | None = None
    found_by: str | None = None
    ordering_ambiguous: bool | None = None
    delta_magnitude: float | None = None
    drop_a: float | None = None
    drop_b: float | None = None
    light_a: float | None = None
    light_b: float | None = None
    light_b_at_d: float | None = None
    light_b_at_r: float | None = None
    measured_drop: float | None = None
    predicted_drop: float | None = None
    star_b: PublicReportStarB | None = None


class PublicReportLightCurve(_Public):
    """The light curve sent with a Tangra JSON report."""

    file: str | None = None
    points: int | None = None
    comparison_stars: int | None = None
    frames_from: int | None = None
    frames_to: int | None = None


class PublicReportPrediction(_Public):
    """Where a Tangra JSON report's event was identified from."""

    source: str | None = None
    owc_event_id: str | None = None


class PublicReportRecording(_Public):
    """When the recording behind a Tangra JSON report starts and ends (UTC)."""

    start_utc: str | None = None
    end_utc: str | None = None


class PublicReportSubmission(_Public):
    """A Tangra JSON report's region and station number."""

    region: str | None = None
    station_number: int | None = None


class PublicReportTimingDetail(_Public):
    """A Tangra JSON report's camera delays and time-base quality."""

    instrumental_delays: str | None = None
    acquisition_delay_ms: float | None = None
    reference_time_to_utc_offset_ms: float | None = None
    delays_entered_at_report: bool | None = None
    embedded_timestamps: bool | None = None
    windows_clock_timestamps: bool | None = None
    fitted_in_time: bool | None = None
    reliable_time_base: bool | None = None
    timestamp_jitter_3sigma_ms: float | None = None
    timestamp_jitter_frames: float | None = None


class PublicReportDetails(_Public):
    """The report's equipment, conditions and timing, as the observer entered it.

    Times are ISO 8601 times of day (UTC); errors are in seconds. The fields
    from ``acknowledged_messages`` on are set for Tangra JSON reports only.
    """

    telescope_type: str | None = None
    aperture: str | None = None
    f_ratio: str | None = None
    detector_model: str | None = None
    integration: str | None = None
    video_format: str | None = None
    timing_method: str | None = None
    timing_device: str | None = None
    timing_source: str | None = None
    ote_used: str | None = None
    clouds: str | None = None
    stability: str | None = None
    started_observing: str | None = None
    stopped_observing: str | None = None
    d_uncorrected: str | None = None
    d_corrected: str | None = None
    r_uncorrected: str | None = None
    r_corrected: str | None = None
    d_err: float | None = None
    d_err_68: float | None = None
    d_err_95: float | None = None
    d_err_9973: float | None = None
    r_err: float | None = None
    r_err_68: float | None = None
    r_err_95: float | None = None
    r_err_9973: float | None = None
    events: list[PublicReportEvent] | None = None
    double_stars: list[PublicReportDoubleStar] | None = None
    light_curve: PublicReportLightCurve | None = None
    prediction: PublicReportPrediction | None = None
    recording: PublicReportRecording | None = None
    submission: PublicReportSubmission | None = None
    timing_detail: PublicReportTimingDetail | None = None
    camera_model_for_delays: str | None = None
    acknowledged_messages: list[str] | None = None
    archive_comment: str | None = None
    observers_more_than_listed: bool | None = None


class PublicReportSite(_Public):
    """Where the observer stood, as the report records it.

    ``None`` on an observation unless the key's owner made it or may view
    observers' personal details.
    """

    observing_location: str | None = None
    latitude: str | None = None
    longitude: str | None = None
    elevation: str | None = None
    datum: str | None = None
    station_latitude: str | None = None
    station_longitude: str | None = None
    station_altitude: str | None = None


class PublicObservation(_Public):
    """An observation (draft or submitted), self-describing for a poller.

    ``status`` is derived by the server: ``draft``, ``submitted``, ``amended``
    (the owner changed a file after submitting) or ``locked`` (its event is in
    review or later, so the owner can no longer change its files) -- an open
    set, like every status here. ``web_url`` is the observation in the web app.
    """

    observation_id: str
    submission_status: str
    status: str = "draft"
    web_url: str | None = None
    amendment_count: int = 0
    amended_at: str | None = None
    submitted_at: str | None = None
    created_at: str
    updated_at: str
    version: int
    created_via: str = "web"
    event_id: str | None = None
    observer_user_id: str
    metadata: PublicObservationMetadata
    files: dict[str, PublicFileVersion | None] = Field(default_factory=dict)
    attachments: list[PublicAttachment] = Field(default_factory=list)
    readiness: PublicReadiness | None = None
    checks: PublicChecks | None = None
    next_actions: list[str] = Field(default_factory=list)
    report_parse: PublicReportParse | None = None
    report_details: PublicReportDetails | None = None
    report_site: PublicReportSite | None = None
    owc: PublicOWC | None = None


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
    attachment_id: str | None = None


class PublicFileList(_Public):
    """``GET /observations/{id}/files``."""

    observation_id: str
    files: list[PublicFileLink] = Field(default_factory=list)
    zip_url: str | None = None
    expires_in: int


class PublicCheckObservation(_Public):
    """The caller's own observation that matched a check."""

    observation_id: str
    status: str
    amendment_count: int
    web_url: str


class PublicCheckEvent(_Public):
    """An event for the checked asteroid + star + date: id and status only."""

    event_id: str
    status: str


class PublicCheckResponse(_Public):
    """Answer of ``POST /observations/check``.

    ``found``: ``observation`` (one of yours; ``event`` is then its attached
    event, if any), ``event`` (none of yours, but the event exists) or
    ``none``. Another user's observation is never returned.
    """

    found: str
    observation: PublicCheckObservation | None = None
    event: PublicCheckEvent | None = None


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


# --------------------------------------------------------------------------
# Key exchange
# --------------------------------------------------------------------------


class PublicKeyExchangeAccount(_Public):
    """The Hub account an exchanged key belongs to."""

    email: str
    display_name: str | None = None


class PublicKeyExchangeResult(_Public):
    """A Hub API key issued for an OccultWatcher identity.

    ``api_key`` is shown only in this response and is left out of ``repr``.
    """

    api_key: str = Field(repr=False)
    key_id: str
    expires_at: str
    scopes: list[str]
    account: PublicKeyExchangeAccount
    client: str


class PublicSignInStarted(_Public):
    """A browser sign-in, started because no Hub account matched.

    Open ``verification_url``, then collect with ``collect_code`` every
    ``interval`` seconds. ``collect_code`` is a secret, left out of ``repr``.
    """

    request_id: str
    collect_code: str = Field(repr=False)
    verification_url: str
    expires_at: str
    expires_in: int
    interval: int
    client: str
