"""Contract: GET /api/v1/billing/config/ matches shared JSON Schema."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from django.test import Client, override_settings
from tests.test_billing_api import _POLYGON

SCHEMA_PATH = (
    Path(__file__).resolve().parent
    / "contracts"
    / "billing-config.response.schema.json"
)


def _load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_billing_config_contract(payload: dict[str, Any]) -> None:
    """Minimal Draft-2020-12 subset validator (no jsonschema dependency)."""
    schema = _load_schema()
    assert schema.get("type") == "object"
    required = schema.get("required", [])
    props = schema.get("properties", {})
    assert isinstance(required, list)
    assert isinstance(props, dict)

    for key in required:
        assert key in payload, f"missing required field: {key}"

    if schema.get("additionalProperties") is False:
        extra = set(payload) - set(props)
        assert not extra, f"unexpected fields: {sorted(extra)}"

    for key, value in payload.items():
        spec = props[key]
        expected = spec.get("type")
        if expected == "integer":
            assert isinstance(value, int) and not isinstance(value, bool), key
            if "minimum" in spec:
                assert value >= spec["minimum"], key
        elif expected == "string":
            assert isinstance(value, str), key
            if "minLength" in spec:
                assert len(value) >= spec["minLength"], key
        elif expected == "boolean":
            assert isinstance(value, bool), key
        else:
            raise AssertionError(f"unsupported schema type for {key}: {expected}")


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, **_POLYGON)
def test_billing_config_matches_shared_contract(client: Client) -> None:
    response = client.get("/api/v1/billing/config/")
    assert response.status_code == 200
    validate_billing_config_contract(response.json())


def test_contract_schema_file_is_present() -> None:
    schema = _load_schema()
    assert schema["title"] == "BillingConfigResponse"
    assert "config_version" in schema["required"]
