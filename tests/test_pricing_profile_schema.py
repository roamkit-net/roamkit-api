"""Tests for PricingProfile schema and lifecycle (ADR 019 / PR1)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.billing.models import Account
from apps.pricing.models import (
    FloorPolicy,
    PricingProfile,
    assign_pricing_profile,
)

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(email="pricing@example.com", password="x")


@pytest.fixture
def account(user) -> Account:
    return Account.objects.get(user=user)


@pytest.mark.django_db
def test_create_profile_defaults():
    profile = PricingProfile.objects.create(
        name="Family",
        slug="family",
        discount_percent=Decimal("5.00"),
    )
    assert profile.version == 1
    assert profile.floor_policy == FloorPolicy.WHOLESALE
    assert profile.archived_at is None
    assert profile.is_active is True


@pytest.mark.django_db
def test_material_change_bumps_version():
    profile = PricingProfile.objects.create(
        name="Family",
        slug="family",
        discount_percent=Decimal("5.00"),
    )
    profile.discount_percent = Decimal("8.00")
    profile.save()
    profile.refresh_from_db()
    assert profile.version == 2


@pytest.mark.django_db
def test_name_only_rename_does_not_bump_version():
    profile = PricingProfile.objects.create(
        name="Family",
        slug="family",
        discount_percent=Decimal("5.00"),
    )
    profile.name = "Friends & Family"
    profile.save()
    profile.refresh_from_db()
    assert profile.version == 1
    assert profile.name == "Friends & Family"


@pytest.mark.django_db
def test_unique_active_slug():
    PricingProfile.objects.create(name="Family", slug="family")
    with pytest.raises(IntegrityError):
        PricingProfile.objects.create(name="Family 2", slug="family")


@pytest.mark.django_db
def test_archived_slug_can_be_reused():
    first = PricingProfile.objects.create(name="Family", slug="family")
    first.archive()
    second = PricingProfile.objects.create(name="Family New", slug="family")
    assert second.pk != first.pk
    assert second.archived_at is None


@pytest.mark.django_db
def test_save_optimistic_conflict():
    profile = PricingProfile.objects.create(
        name="Family",
        slug="family",
        discount_percent=Decimal("5.00"),
    )
    profile.discount_percent = Decimal("10.00")
    ok = profile.save_optimistic(
        expected_version=1,
        update_fields=["discount_percent"],
    )
    assert ok is True
    profile.refresh_from_db()
    assert profile.version == 2

    stale = PricingProfile.objects.get(pk=profile.pk)
    stale.discount_percent = Decimal("12.00")
    ok2 = stale.save_optimistic(
        expected_version=1,
        update_fields=["discount_percent"],
    )
    assert ok2 is False


@pytest.mark.django_db
def test_assign_pricing_profile(account):
    profile = PricingProfile.objects.create(
        name="Family",
        slug="family",
        discount_percent=Decimal("5.00"),
    )
    updated = assign_pricing_profile(
        account_ids=[account.pk],
        profile=profile,
        reason="bulk_assign",
    )
    assert updated == 1
    account.refresh_from_db()
    assert account.pricing_profile_id == profile.pk


@pytest.mark.django_db
def test_assign_rejects_over_100(account):
    profile = PricingProfile.objects.create(name="Family", slug="family")
    ids = [account.pk] * 101
    with pytest.raises(ValidationError):
        assign_pricing_profile(account_ids=ids, profile=profile)


@pytest.mark.django_db
def test_discount_percent_constraint():
    with pytest.raises(IntegrityError):
        PricingProfile.objects.create(
            name="Bad",
            slug="bad",
            discount_percent=Decimal("150.00"),
        )


@pytest.mark.django_db
def test_settings_flag_default_false(settings):
    assert settings.PRICING_PROFILES_ENABLED is False
