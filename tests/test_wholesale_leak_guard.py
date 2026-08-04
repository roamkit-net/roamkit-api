"""Guard: wholesale / cost fields must never leak via public API surfaces."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from rest_framework import serializers

from apps.catalog.serializers import LocationListSerializer, PackageSerializer
from apps.esims.serializers import (
    EsimSerializer,
    TopupPackageSerializer,
    TopupSerializer,
)
from apps.orders.serializers import OrderSerializer

REPO_ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = REPO_ROOT / "openapi" / "openapi.yaml"

# Substrings that must not appear as public response field names.
FORBIDDEN_FIELD_SUBSTRINGS = (
    "net_price",
    "net_price_usd",
    "margin",
    "provider_cost",
    "wholesale",
)

# Exact field names that imply cost (``cost`` alone is too broad for words like
# ``customer``; require boundary matches).
FORBIDDEN_FIELD_EXACT = frozenset(
    {
        "cost",
        "net_price",
        "net_price_usd",
        "margin",
        "provider_cost",
        "wholesale",
    }
)

PUBLIC_SERIALIZERS: tuple[type[serializers.BaseSerializer], ...] = (
    PackageSerializer,
    LocationListSerializer,
    EsimSerializer,
    OrderSerializer,
    TopupPackageSerializer,
    TopupSerializer,
)


def _serializer_field_names(ser_cls: type[serializers.BaseSerializer]) -> set[str]:
    instance = ser_cls()
    return set(instance.fields.keys())


def _forbidden_hits(names: set[str]) -> list[str]:
    hits: list[str] = []
    for name in sorted(names):
        lowered = name.lower()
        if lowered in FORBIDDEN_FIELD_EXACT:
            hits.append(name)
            continue
        for needle in FORBIDDEN_FIELD_SUBSTRINGS:
            if needle in lowered:
                hits.append(name)
                break
    return hits


@pytest.mark.parametrize("ser_cls", PUBLIC_SERIALIZERS)
def test_public_serializers_omit_wholesale_fields(
    ser_cls: type[serializers.BaseSerializer],
) -> None:
    hits = _forbidden_hits(_serializer_field_names(ser_cls))
    assert not hits, f"{ser_cls.__name__} exposes wholesale fields: {hits}"


def test_openapi_schemas_omit_wholesale_properties() -> None:
    schema = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    components = (schema.get("components") or {}).get("schemas") or {}
    bad: list[str] = []
    for schema_name, body in components.items():
        if not isinstance(body, dict):
            continue
        props = body.get("properties") or {}
        if not isinstance(props, dict):
            continue
        hits = _forbidden_hits(set(props.keys()))
        for hit in hits:
            bad.append(f"{schema_name}.{hit}")
    assert not bad, f"OpenAPI schemas expose wholesale fields: {bad}"


def test_openapi_document_has_no_wholesale_property_keys() -> None:
    """Belt-and-suspenders: raw YAML must not declare forbidden property keys."""
    text = OPENAPI_PATH.read_text(encoding="utf-8")
    for needle in ("net_price_usd", "net_price", "provider_cost"):
        # Match OpenAPI property keys like "net_price_usd:" at line start / indent.
        pattern = re.compile(rf"(?m)^\s+{re.escape(needle)}\s*:")
        assert not pattern.search(text), f"openapi.yaml declares property {needle!r}"
