"""Wallet cutover Data Migration Gate / backfill (ADR 018 Phase 0)."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import override_settings

from apps.wallet.models import WalletAddress, WalletIdentity
from apps.wallet.services.backfill import run_wallet_backfill, validate_wallet_migration

User = get_user_model()

_TEST_MNEMONIC = "test test test test test test test test test test test junk"


@pytest.mark.django_db
@override_settings(WALLET_HD_MNEMONIC=_TEST_MNEMONIC)
def test_backfill_dry_run_does_not_allocate() -> None:
    User.objects.create_user(email="bf-dry@example.com", password="secret123")
    report = run_wallet_backfill(apply=False)

    assert report.mode == "dry-run"
    assert report.would_allocate == 1
    assert report.allocated == 0
    assert WalletIdentity.objects.count() == 0
    assert WalletAddress.objects.count() == 0


@pytest.mark.django_db
@override_settings(WALLET_HD_MNEMONIC=_TEST_MNEMONIC)
def test_backfill_apply_allocates_and_is_idempotent() -> None:
    user = User.objects.create_user(email="bf-apply@example.com", password="secret123")
    first = run_wallet_backfill(apply=True)
    second = run_wallet_backfill(apply=True)

    assert first.allocated == 1
    assert first.errors == 0
    assert second.would_allocate == 0
    assert second.allocated == 0
    assert WalletIdentity.objects.filter(account=user.billing_account).count() == 1
    assert WalletAddress.objects.filter(status="active").count() == 1


@pytest.mark.django_db
@override_settings(WALLET_HD_MNEMONIC=_TEST_MNEMONIC)
def test_validation_fails_when_accounts_missing() -> None:
    User.objects.create_user(email="bf-miss@example.com", password="secret123")
    report = validate_wallet_migration(sample_size=0)

    assert report.passed is False
    assert report.accounts_missing_identity == 1
    assert report.accounts_missing_active_address == 1


@pytest.mark.django_db
@override_settings(WALLET_HD_MNEMONIC=_TEST_MNEMONIC)
def test_validation_passes_after_backfill() -> None:
    User.objects.create_user(email="bf-ok@example.com", password="secret123")
    User.objects.create_user(email="bf-ok2@example.com", password="secret123")
    run_wallet_backfill(apply=True)
    report = validate_wallet_migration(sample_size=5)

    assert report.as_dict()["passed"] is True
    assert report.orphan_identities == 0
    assert report.duplicate_active_addresses == 0
    assert report.missing_index_registry_fields == 0
    assert report.sample_mismatches == 0
    assert report.sample_checked == 2


@pytest.mark.django_db
@override_settings(WALLET_HD_MNEMONIC=_TEST_MNEMONIC)
def test_management_command_validate_after_apply(capsys) -> None:
    User.objects.create_user(email="bf-cmd@example.com", password="secret123")
    call_command(
        "wallet_backfill_addresses",
        "--apply",
        "--validate",
        "--sample-size=1",
    )
    out = capsys.readouterr().out
    assert "backfill mode=apply" in out
    assert "validation=PASS" in out
