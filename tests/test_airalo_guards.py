"""Tests for Airalo config guards and fail-closed client."""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from apps.integrations.airalo.client import AiraloClient, AiraloClientError
from config.settings.airalo_guards import (
    parse_blocked_client_ids,
    validate_production_airalo,
    validate_staging_airalo,
)


def test_parse_blocked_client_ids_splits_and_strips() -> None:
    assert parse_blocked_client_ids(" a ,b, ,c ") == frozenset({"a", "b", "c"})
    assert parse_blocked_client_ids("") == frozenset()
    assert parse_blocked_client_ids(None) == frozenset()


def test_staging_rejects_sandbox_false() -> None:
    with pytest.raises(ImproperlyConfigured, match="AIRALO_SANDBOX must be true"):
        validate_staging_airalo(
            airalo_sandbox=False,
            airalo_enabled=False,
            client_id="",
            client_secret="",
            blocked_client_ids=frozenset(),
        )


def test_staging_rejects_blocked_client_id() -> None:
    with pytest.raises(ImproperlyConfigured, match="blocked on staging"):
        validate_staging_airalo(
            airalo_sandbox=True,
            airalo_enabled=True,
            client_id="live-fine-star",
            client_secret="secret",
            blocked_client_ids=frozenset({"live-fine-star"}),
        )


def test_staging_rejects_enabled_without_credentials() -> None:
    with pytest.raises(ImproperlyConfigured, match="must both be set"):
        validate_staging_airalo(
            airalo_sandbox=True,
            airalo_enabled=True,
            client_id="",
            client_secret="",
            blocked_client_ids=frozenset(),
        )


def test_staging_allows_disabled_with_empty_credentials() -> None:
    validate_staging_airalo(
        airalo_sandbox=True,
        airalo_enabled=False,
        client_id="",
        client_secret="",
        blocked_client_ids=frozenset({"live-fine-star"}),
    )


def test_staging_allows_sandbox_credentials() -> None:
    validate_staging_airalo(
        airalo_sandbox=True,
        airalo_enabled=True,
        client_id="sandbox-id",
        client_secret="sandbox-secret",
        blocked_client_ids=frozenset({"live-fine-star"}),
    )


def test_production_rejects_sandbox_true() -> None:
    with pytest.raises(ImproperlyConfigured, match="AIRALO_SANDBOX must be false"):
        validate_production_airalo(
            airalo_sandbox=True,
            airalo_enabled=True,
            client_id="prod-id",
            client_secret="prod-secret",
        )


def test_production_rejects_enabled_false() -> None:
    with pytest.raises(ImproperlyConfigured, match="AIRALO_ENABLED must be true"):
        validate_production_airalo(
            airalo_sandbox=False,
            airalo_enabled=False,
            client_id="prod-id",
            client_secret="prod-secret",
        )


def test_production_rejects_missing_credentials() -> None:
    with pytest.raises(ImproperlyConfigured, match="must both be configured"):
        validate_production_airalo(
            airalo_sandbox=False,
            airalo_enabled=True,
            client_id="",
            client_secret="prod-secret",
        )


def test_production_allows_consistent_live_config() -> None:
    validate_production_airalo(
        airalo_sandbox=False,
        airalo_enabled=True,
        client_id="prod-id",
        client_secret="prod-secret",
    )


@override_settings(AIRALO_ENABLED=False, AIRALO_CLIENT_ID="x", AIRALO_CLIENT_SECRET="y")
def test_client_fail_closed_when_disabled() -> None:
    client = AiraloClient()
    with pytest.raises(AiraloClientError, match="AIRALO_ENABLED=false"):
        client.list_packages()


@override_settings(AIRALO_ENABLED=True, AIRALO_CLIENT_ID="", AIRALO_CLIENT_SECRET="")
def test_client_fail_closed_when_credentials_missing() -> None:
    client = AiraloClient()
    with pytest.raises(AiraloClientError, match="credentials are not configured"):
        client.create_order(package_id="pkg-1")
