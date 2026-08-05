"""Architecture tests: OpenAPI covers 100% of /api/v1/ and /api/internal/."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from django.urls import URLPattern, URLResolver, get_resolver
from drf_spectacular.generators import SchemaGenerator

REPO_ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = REPO_ROOT / "openapi" / "openapi.yaml"

OPERATION_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
AUTO_SUFFIX_RE = re.compile(r"_\d+$")

# Endpoints that must appear without bearerAuth in the schema.
PUBLIC_OPERATION_IDS = frozenset(
    {
        "auth_register",
        "auth_activate",
        "auth_password_reset_request",
        "auth_password_reset_confirm",
        "auth_login",
        "auth_refresh",
        "auth_google",
        "billing_config",
        "catalog_packages_list",
        "catalog_locations_list",
        "catalog_locations_retrieve",
    }
)

_API_PREFIXES = ("api/v1/", "api/internal/")


def _normalize_path(path: str) -> str:
    path = "/" + path.lstrip("/")
    if not path.endswith("/"):
        path += "/"
    # Django converters → OpenAPI style
    path = re.sub(r"<(?:[^:]+:)?([^>]+)>", r"{\1}", path)
    # DRF/spectacular commonly expose pk lookups as {id}
    path = path.replace("{pk}", "{id}")
    return path


def _collect_documented_api_paths(patterns=None, prefix: str = "") -> set[str]:
    if patterns is None:
        patterns = get_resolver().url_patterns
    found: set[str] = set()
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            found |= _collect_documented_api_paths(
                pattern.url_patterns, prefix + str(pattern.pattern)
            )
        elif isinstance(pattern, URLPattern):
            full = prefix + str(pattern.pattern)
            if full.startswith(_API_PREFIXES):
                found.add(_normalize_path(full))
    return found


def _load_committed_schema() -> dict:
    return yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))


def _iter_operations(schema: dict):
    for path, methods in (schema.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.startswith("x-") or not isinstance(op, dict):
                continue
            if "operationId" not in op:
                continue
            yield path, method, op


@pytest.mark.django_db
def test_openapi_path_coverage_is_complete() -> None:
    """Documented API routes (/api/v1/, /api/internal/) must match OpenAPI."""
    django_paths = _collect_documented_api_paths()
    schema = SchemaGenerator().get_schema(request=None, public=True)
    openapi_paths = {
        _normalize_path(p) if not p.endswith("/") else p
        for p in (schema.get("paths") or {})
    }
    # spectacular may omit trailing slash inconsistently — normalize both.
    openapi_paths = {_normalize_path(p.rstrip("/") + "/") for p in openapi_paths}

    missing_in_schema = sorted(django_paths - openapi_paths)
    extra_in_schema = sorted(openapi_paths - django_paths)
    assert not missing_in_schema, f"Routes missing from OpenAPI: {missing_in_schema}"
    assert (
        not extra_in_schema
    ), f"OpenAPI paths without Django routes: {extra_in_schema}"


def test_committed_openapi_matches_generator() -> None:
    generated = SchemaGenerator().get_schema(request=None, public=True)
    # Compare path keys only here; full YAML drift is enforced in CI generate script.
    committed = _load_committed_schema()
    assert set(generated.get("paths") or {}) == set(committed.get("paths") or {})


def test_operation_ids_are_stable_and_explicit() -> None:
    schema = _load_committed_schema()
    ids = [op["operationId"] for _, _, op in _iter_operations(schema)]
    assert ids, "schema has no operations"
    assert len(ids) == len(set(ids)), f"duplicate operationIds: {ids}"
    for op_id in ids:
        assert OPERATION_ID_RE.match(op_id), f"invalid operationId: {op_id}"
        assert not AUTO_SUFFIX_RE.search(op_id), f"auto-suffix operationId: {op_id}"


def test_security_documented_for_non_public_operations() -> None:
    schema = _load_committed_schema()
    for path, method, op in _iter_operations(schema):
        op_id = op["operationId"]
        security = op.get("security")
        requires_bearer = False
        if security:
            requires_bearer = any("bearerAuth" in (item or {}) for item in security)

        if op_id in PUBLIC_OPERATION_IDS:
            assert (
                not requires_bearer
            ), f"{op_id} ({method.upper()} {path}) must be public in OpenAPI"
        else:
            assert (
                requires_bearer
            ), f"{op_id} ({method.upper()} {path}) must declare bearerAuth"


def test_public_allowlist_is_exhaustive() -> None:
    schema = _load_committed_schema()
    ids = {op["operationId"] for _, _, op in _iter_operations(schema)}
    unknown = PUBLIC_OPERATION_IDS - ids
    assert not unknown, f"PUBLIC_OPERATION_IDS references missing ops: {unknown}"
