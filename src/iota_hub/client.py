"""The transport layer: one method per public ``operation_id``.

Each method is named by dropping the ``public_`` prefix from the operation id in
``spec/openapi.json`` (``public_create_draft`` → :meth:`Client.create_draft`),
takes the path and query parameters that operation documents, and returns the
response model that operation declares. Nothing here decides anything: mapping a
folder to slots, polling a check run, retrying a submit are the *workflow*
layer's job (design D15), and that layer is built on this one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from time import sleep as _default_sleep
from typing import Any
from urllib.parse import quote

import httpx

from ._http import DEFAULT_BASE_URL, Transport
from .models import (
    PublicChecks,
    PublicDeclaredFile,
    PublicDraftCreated,
    PublicEvent,
    PublicEventFileList,
    PublicEventList,
    PublicFileList,
    PublicObservation,
    PublicObservationList,
    PublicUploadTarget,
)
from .workflow import WorkflowMixin


class Client(WorkflowMixin):
    """A connection to one IOTA Hub deployment, authenticated by one API key.

    ``base_url`` is the API base; ``/public/v1`` is appended (design D16).
    Pass a pre-built :class:`~iota_hub._http.Transport` as ``http`` to share one
    connection pool, or to install a test transport.
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 30.0,
        max_retries: int = 4,
        transport: httpx.BaseTransport | None = None,
        user_agent: str | None = None,
        sleep: Callable[[float], None] = _default_sleep,
        http: Transport | None = None,
    ) -> None:
        self.http = http or Transport(
            api_key,
            base_url,
            timeout=timeout,
            max_retries=max_retries,
            transport=transport,
            user_agent=user_agent,
            sleep=sleep,
        )

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---------------------------------------------------------------- writes

    def create_draft(
        self,
        files: list[PublicDeclaredFile] | list[dict[str, Any]] | None = None,
        *,
        auto_submit_when_clean: bool = False,
        idempotency_key: str | None = None,
    ) -> PublicDraftCreated:
        """``public_create_draft`` — create a draft and presign its uploads."""
        body = {
            "files": [_as_dict(item) for item in files or []],
            "auto_submit_when_clean": auto_submit_when_clean,
        }
        response = self.http.request(
            "POST",
            "/observations/drafts",
            json=body,
            idempotency_key=idempotency_key,
        )
        return PublicDraftCreated.model_validate(response.json())

    def finalize_draft(
        self, observation_id: str, *, idempotency_key: str | None = None
    ) -> PublicObservation:
        """``public_finalize_draft`` — commit the uploaded files. No body."""
        response = self.http.request(
            "POST",
            f"/observations/{_seg(observation_id)}/finalize",
            idempotency_key=idempotency_key,
        )
        return PublicObservation.model_validate(response.json())

    def init_file_upload(
        self,
        observation_id: str,
        slot: str,
        *,
        filename: str,
        sha256: str | None = None,
    ) -> PublicUploadTarget:
        """``public_init_file_upload`` — presign one slot's replacement file."""
        response = self.http.request(
            "POST",
            f"/observations/{_seg(observation_id)}/files/{_seg(slot)}/upload/init",
            json={"filename": filename, "sha256": sha256},
        )
        return PublicUploadTarget.model_validate(response.json())

    def finalize_file_upload(
        self,
        observation_id: str,
        slot: str,
        *,
        file_key: str,
        filename: str,
        idempotency_key: str | None = None,
    ) -> PublicObservation:
        """``public_finalize_file_upload`` — commit one uploaded file."""
        response = self.http.request(
            "POST",
            f"/observations/{_seg(observation_id)}/files/{_seg(slot)}/upload/finalize",
            json={"file_key": file_key, "filename": filename},
            idempotency_key=idempotency_key,
        )
        return PublicObservation.model_validate(response.json())

    def delete_file(self, observation_id: str, slot: str) -> PublicObservation:
        """``public_delete_file`` — clear one slot."""
        response = self.http.request(
            "DELETE", f"/observations/{_seg(observation_id)}/files/{_seg(slot)}"
        )
        return PublicObservation.model_validate(response.json())

    def run_checks(self, observation_id: str) -> PublicChecks:
        """``public_run_checks`` — start an observer check run (``202``)."""
        response = self.http.request(
            "POST", f"/observations/{_seg(observation_id)}/validation/runs"
        )
        return PublicChecks.model_validate(response.json())

    def dismiss_finding(
        self, observation_id: str, fingerprint: str, *, note: str
    ) -> PublicChecks:
        """``public_dismiss_finding`` — dismiss one finding with a note."""
        response = self.http.request(
            "POST",
            f"/observations/{_seg(observation_id)}"
            f"/validation/findings/{_seg(fingerprint)}/dismiss",
            json={"note": note},
        )
        return PublicChecks.model_validate(response.json())

    def undo_dismissal(self, observation_id: str, fingerprint: str) -> PublicChecks:
        """``public_undo_dismissal`` — undo one dismissal."""
        response = self.http.request(
            "DELETE",
            f"/observations/{_seg(observation_id)}"
            f"/validation/findings/{_seg(fingerprint)}/dismiss",
        )
        return PublicChecks.model_validate(response.json())

    def submit_draft(
        self,
        observation_id: str,
        *,
        version: int,
        idempotency_key: str | None = None,
    ) -> PublicObservation:
        """``public_submit_draft`` — submit, echoing the version you last read."""
        response = self.http.request(
            "POST",
            f"/observations/{_seg(observation_id)}/submit",
            json={"version": version},
            idempotency_key=idempotency_key,
        )
        return PublicObservation.model_validate(response.json())

    def delete_draft(self, observation_id: str) -> None:
        """``public_delete_draft`` — throw away an unsubmitted draft (``204``)."""
        self.http.request("DELETE", f"/observations/{_seg(observation_id)}")

    # ----------------------------------------------------------------- reads

    def list_events(
        self,
        *,
        status: str | list[str] | None = None,
        asteroid_id: str | None = None,
        asteroid_name: str | None = None,
        star_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        observer_names: list[str] | None = None,
        waiting_for: str | None = None,
        assigned_reviewer_id: list[str] | None = None,
        sort_by: str | None = None,
        sort_order: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> PublicEventList:
        """``public_list_events`` — one page of events."""
        response = self.http.request(
            "GET",
            "/events",
            params={
                "status": _comma(status),
                "asteroid_id": asteroid_id,
                "asteroid_name": asteroid_name,
                "star_id": star_id,
                "date_from": date_from,
                "date_to": date_to,
                "observer_names": observer_names,
                "waiting_for": waiting_for,
                "assigned_reviewer_id": assigned_reviewer_id,
                "sort_by": sort_by,
                "sort_order": sort_order,
                "limit": limit,
                "cursor": cursor,
            },
        )
        return PublicEventList.model_validate(response.json())

    def get_event(self, event_id: str) -> PublicEvent:
        """``public_get_event``."""
        response = self.http.request("GET", f"/events/{_seg(event_id)}")
        return PublicEvent.model_validate(response.json())

    def list_event_files(self, event_id: str) -> PublicEventFileList:
        """``public_list_event_files`` — presigned download links."""
        response = self.http.request("GET", f"/events/{_seg(event_id)}/files")
        return PublicEventFileList.model_validate(response.json())

    def list_observations(
        self,
        *,
        observer_names: list[str] | None = None,
        event_status_group: str | None = None,
        submission_status: str | None = None,
        asteroid_id: str | None = None,
        asteroid_name: str | None = None,
        star_id: str | None = None,
        result_type: list[str] | None = None,
        ungrouped_only: bool | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort_by: str | None = None,
        sort_order: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> PublicObservationList:
        """``public_list_observations`` — one page of observations."""
        response = self.http.request(
            "GET",
            "/observations",
            params={
                "observer_names": observer_names,
                "event_status_group": event_status_group,
                "submission_status": submission_status,
                "asteroid_id": asteroid_id,
                "asteroid_name": asteroid_name,
                "star_id": star_id,
                "result_type": result_type,
                "ungrouped_only": ungrouped_only,
                "date_from": date_from,
                "date_to": date_to,
                "sort_by": sort_by,
                "sort_order": sort_order,
                "limit": limit,
                "cursor": cursor,
            },
        )
        return PublicObservationList.model_validate(response.json())

    def get_observation(self, observation_id: str) -> PublicObservation:
        """``public_get_observation`` — the resource a client polls."""
        response = self.http.request("GET", f"/observations/{_seg(observation_id)}")
        return PublicObservation.model_validate(response.json())

    def list_observation_files(self, observation_id: str) -> PublicFileList:
        """``public_list_observation_files`` — presigned download links."""
        response = self.http.request(
            "GET", f"/observations/{_seg(observation_id)}/files"
        )
        return PublicFileList.model_validate(response.json())

    # ------------------------------------------------------------ pagination

    def iter_events(self, **filters: Any) -> Iterator[PublicEvent]:
        """Every event matching the filters, following ``next_cursor``."""
        cursor: str | None = filters.pop("cursor", None)
        while True:
            page = self.list_events(cursor=cursor, **filters)
            yield from page.items
            cursor = page.next_cursor
            if not cursor:
                return

    def iter_observations(self, **filters: Any) -> Iterator[PublicObservation]:
        """Every observation matching the filters, following ``next_cursor``."""
        cursor: str | None = filters.pop("cursor", None)
        while True:
            page = self.list_observations(cursor=cursor, **filters)
            yield from page.items
            cursor = page.next_cursor
            if not cursor:
                return

    # --------------------------------------------------------------------
    # The workflow layer (design D15, docs/client-conventions.md) is mixed in
    # from ``iota_hub/workflow.py``: submit_folder / submit_files,
    # wait_for_checks, replace_file, download_files. It is hand-written on top
    # of the methods above, and nothing above this line may grow a policy
    # decision.
    # --------------------------------------------------------------------


def _seg(value: str) -> str:
    """One path segment, escaped."""
    return quote(str(value), safe="")


def _comma(value: str | list[str] | None) -> str | None:
    """``status`` is comma-separated for OR, not a repeated parameter."""
    if isinstance(value, list):
        return ",".join(value) or None
    return value


def _as_dict(item: Any) -> dict[str, Any]:
    return item.model_dump() if hasattr(item, "model_dump") else dict(item)
