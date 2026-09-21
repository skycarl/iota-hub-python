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
