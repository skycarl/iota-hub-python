"""The models are checked against ``spec/openapi.json``, not against prose.

If the vendored document grows a schema, a property or a required field that
these models do not have, the first test here fails and says which one. That is
the model side of the contract-drift guard; ``scripts/check_openapi_drift.py``
is the other side, and keeps the vendored document honest.
"""

from __future__ import annotations

import json

import pytest
from conftest import SPEC_PATH

from iota_hub import models

SCHEMAS: dict[str, dict] = json.loads(SPEC_PATH.read_text())["components"]["schemas"]
PUBLIC_SCHEMAS = sorted(name for name in SCHEMAS if name.startswith("Public"))


def test_the_document_holds_only_public_schemas():
    assert PUBLIC_SCHEMAS, "no schemas found - is spec/openapi.json the public one?"
    assert len(PUBLIC_SCHEMAS) == len(SCHEMAS), "a non-Public schema leaked in"


@pytest.mark.parametrize("name", PUBLIC_SCHEMAS)
def test_every_schema_has_a_model_with_the_same_fields(name: str):
    model = getattr(models, name, None)
    assert model is not None, f"{name} is in the OpenAPI document but not in models.py"

    properties = set(SCHEMAS[name].get("properties", {}))
    fields = set(model.model_fields)
    assert properties - fields == set(), f"{name}: fields missing from the model"
    assert fields - properties == set(), f"{name}: fields the schema does not have"


@pytest.mark.parametrize("name", PUBLIC_SCHEMAS)
def test_required_matches_the_schema(name: str):
    model = getattr(models, name)
    required = set(SCHEMAS[name].get("required", []))
    actual = {field for field, info in model.model_fields.items() if info.is_required()}
    assert actual == required


def test_unknown_fields_are_ignored():
    """An additive API change must never break an installed client."""
    parsed = models.PublicReadiness.model_validate(
        {"state": "ready", "some_field_added_in_2027": {"nested": [1, 2, 3]}}
    )
    assert parsed.state == "ready"
    assert not hasattr(parsed, "some_field_added_in_2027")


def test_a_new_next_actions_verb_and_readiness_state_still_parse():
    """Open string enums, for the same reason."""
    observation = models.PublicObservation.model_validate(
        {
            "observation_id": "obs_1",
            "submission_status": "draft",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "version": 1,
            "observer_user_id": "user_1",
            "metadata": {},
            "readiness": {"state": "awaiting_coordinator"},
            "checks": {"run_id": "run_1", "status": "queued"},
            "next_actions": ["upload:vizier", "call_a_coordinator"],
        }
    )
    assert observation.readiness.state == "awaiting_coordinator"
    assert observation.checks.status == "queued"
    assert observation.next_actions == ["upload:vizier", "call_a_coordinator"]


def test_declared_file_slots_are_closed():
    """The slot set is the one thing a request may not invent."""
    with pytest.raises(ValueError):
        models.PublicDeclaredFile(slot="notes", filename="notes.txt", size=1)


def test_defaults_match_the_schema():
    checks = models.PublicChecks(run_id="run_1", status="running")
    assert checks.findings == []
    assert checks.open_findings == 0
    assert checks.checks_current is False

    evidence = models.PublicEvidence(source="s", field="f", expected="a", actual="b")
    assert evidence.expected_label == "Expected"
    assert evidence.mismatch is True


def test_the_derived_status_web_url_and_amendments_parse_and_default():
    minimal = {
        "observation_id": "obs_1",
        "submission_status": "submitted",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "version": 3,
        "observer_user_id": "user_1",
        "metadata": {},
    }
    # An older server sends none of them: the defaults are the schema's.
    observation = models.PublicObservation.model_validate(minimal)
    assert observation.status == "draft"
    assert observation.web_url is None
    assert observation.amendment_count == 0
    assert observation.amended_at is None
    assert observation.metadata.station is None

    observation = models.PublicObservation.model_validate(
        {
            **minimal,
            "status": "amended",
            "web_url": "https://hub.example.test/observations/obs_1",
            "amendment_count": 2,
            "amended_at": "2026-02-01T00:00:00Z",
            "metadata": {"station": "2"},
        }
    )
    assert observation.status == "amended"
    assert observation.web_url.endswith("/observations/obs_1")
    assert observation.amendment_count == 2
    assert observation.metadata.station == "2"

    # A status the API adds later still parses (open string enum).
    assert (
        models.PublicObservation.model_validate(
            {**minimal, "status": "withdrawn"}
        ).status
        == "withdrawn"
    )


def test_the_submit_version_is_optional():
    assert models.PublicSubmitRequest().version is None
    assert models.PublicSubmitRequest(version=7).version == 7


def test_the_aperture_record_is_a_declarable_slot():
    declared = models.PublicDeclaredFile(
        slot="aperture_record", filename="x.pymovie", size=8
    )
    assert declared.slot == "aperture_record"


def test_the_check_request_and_every_answer_parse():
    request = models.PublicCheckRequest(
        asteroid_name="Doty", star_id="1-2", observed_date="2018-03-05"
    )
    assert request.asteroid_id is None
    assert request.star_catalog is None
    assert request.station is None

    none = models.PublicCheckResponse.model_validate({"found": "none"})
    assert none.observation is None and none.event is None

    event = models.PublicCheckResponse.model_validate(
        {"found": "event", "event": {"event_id": "evt_1", "status": "IN_REVIEW"}}
    )
    assert event.event.event_id == "evt_1"
    assert event.observation is None

    found = models.PublicCheckResponse.model_validate(
        {
            "found": "observation",
            "observation": {
                "observation_id": "obs_1",
                "status": "draft",
                "amendment_count": 0,
                "web_url": "https://hub.example.test/new-observation?draft=obs_1",
                "added_later": True,
            },
            "event": None,
        }
    )
    assert found.observation.status == "draft"
    assert found.observation.web_url.endswith("draft=obs_1")
