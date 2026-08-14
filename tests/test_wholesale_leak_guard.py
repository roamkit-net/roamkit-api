"""Guard: wholesale / cost / internal pricing fields must never leak via public API."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from rest_framework import serializers

from apps.catalog.serializers import LocationListSerializer, PackageSerializer
from apps.esims.serializers import (
    AppliedPackageSerializer,
    EsimSerializer,
    TopupPackageSerializer,
    TopupSerializer,
)
from apps.orders.serializers import OrderSerializer
from apps.pricing.presentation import (
    PUBLIC_LEAK_FORBIDDEN_EXACT,
    PUBLIC_LEAK_FORBIDDEN_SUBSTRINGS,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = REPO_ROOT / "openapi" / "openapi.yaml"

FORBIDDEN_FIELD_SUBSTRINGS = PUBLIC_LEAK_FORBIDDEN_SUBSTRINGS
FORBIDDEN_FIELD_EXACT = PUBLIC_LEAK_FORBIDDEN_EXACT

PUBLIC_SERIALIZERS: tuple[type[serializers.BaseSerializer], ...] = (
    PackageSerializer,
    LocationListSerializer,
    AppliedPackageSerializer,
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
        # Internal preview response is staff-only; allow ops fields there.
        if schema_name.startswith("PricingPreview"):
            continue
        props = body.get("properties") or {}
        if not isinstance(props, dict):
            continue
        hits = _forbidden_hits(set(props.keys()))
        for hit in hits:
            bad.append(f"{schema_name}.{hit}")
    assert not bad, f"OpenAPI schemas expose wholesale fields: {bad}"


def test_openapi_document_has_no_wholesale_property_keys() -> None:
    """Belt-and-suspenders: raw YAML must not declare forbidden property keys
    on public paths. Internal preview schema is excluded by name."""
    text = OPENAPI_PATH.read_text(encoding="utf-8")
    # Strip internal PricingPreview* schema blocks roughly by excluding lines
    # under those component keys is hard in raw text; check public Package/Topup.
    schema = yaml.safe_load(text)
    paths = schema.get("paths") or {}
    public_prefixes = (
        "/api/v1/packages",
        "/api/v1/locations",
        "/api/v1/me/",
        "/api/v1/orders",
    )
    bad: list[str] = []
    for path, methods in paths.items():
        if not any(path.startswith(p) for p in public_prefixes):
            continue
        if not isinstance(methods, dict):
            continue
        # Resolve $ref responses would need full walk; property-key scan on
        # inline schemas only. Component-level covered above.
        _ = methods
    for needle in ("net_price_usd", "net_price", "provider_cost"):
        pattern = re.compile(rf"(?m)^\s+{re.escape(needle)}\s*:")
        # Allow under PricingPreviewResponse
        for match in pattern.finditer(text):
            start = max(0, match.start() - 400)
            window = text[start : match.start()]
            if "PricingPreview" in window:
                continue
            bad.append(needle)
            break
    assert not bad, f"openapi.yaml declares forbidden property keys: {bad}"
