"""ADR 019 PR4 — public catalog pricing + internal preview + leak contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken

from apps.billing.services import ensure_billing_account
from apps.catalog.models import Package
from apps.pricing.models import FloorPolicy, PricingProfile
from apps.pricing.presentation import (
    public_price_dict,
    resolve_package_quote,
    resolve_preview_quote,
)
from apps.pricing.types import OrderType, PricingReason

User = get_user_model()
PASSWORD = "SecurePass1!"


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def package() -> Package:
    return Package.objects.create(
        external_id="pkg-preview-57",
        title="10 GB - 180 Days",
        operator_title="Change",
        operator_id="op-1",
        country_code="US",
        data_allowance="10 GB",
        validity_days=180,
        price_usd=Decimal("57.00"),
        net_price_usd=Decimal("50.00"),
        is_unlimited=False,
        plan_type="data",
        is_active=True,
        synced_at=datetime.now(UTC),
    )


@pytest.fixture
def family_user(package: Package) -> tuple:
    user = User.objects.create_user(email="family-api@example.com", password=PASSWORD)
    user.is_active = True
    user.save()
    profile = PricingProfile.objects.create(
        name="Family",
        slug="family-api",
        discount_percent=Decimal("5.00"),
        floor_policy=FloorPolicy.WHOLESALE,
        is_active=True,
    )
    account = ensure_billing_account(user)
    account.pricing_profile = profile
    account.save(update_fields=["pricing_profile", "updated_at"])
    return user, account, profile


def _auth_header(user) -> dict[str, str]:
    access = str(RefreshToken.for_user(user).access_token)
    return {"HTTP_AUTHORIZATION": f"Bearer {access}"}


@pytest.mark.django_db
def test_packages_anonymous_retail_fields(
    client: Client, package: Package, settings
) -> None:
    settings.PRICING_PROFILES_ENABLED = True
    response = client.get("/api/v1/packages/")
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["price_usd"] == "57.00"
    assert result["list_price_usd"] == "57.00"
    assert result["discount_percent"] == "0.00"
    assert result["pricing_reason"] == PricingReason.RETAIL
    assert "net_price_usd" not in result
    assert "quote_fingerprint" not in result
    assert "floor_reason" not in result
    assert "pricing_context_hash" not in result


@pytest.mark.django_db
def test_packages_authenticated_family_discount(
    client: Client, package: Package, family_user, settings
) -> None:
    settings.PRICING_PROFILES_ENABLED = True
    user, _account, _profile = family_user
    response = client.get("/api/v1/packages/", **_auth_header(user))
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["list_price_usd"] == "57.00"
    assert result["price_usd"] == "54.15"
    assert result["discount_percent"] == "5.00"
    assert result["pricing_reason"] == PricingReason.PRICING_PROFILE
    assert "net_price" not in result
    assert "pricing_profile_id" not in result


@pytest.mark.django_db
def test_packages_flag_off_ignores_profile(
    client: Client, package: Package, family_user, settings
) -> None:
    settings.PRICING_PROFILES_ENABLED = False
    user, _account, _profile = family_user
    response = client.get("/api/v1/packages/", **_auth_header(user))
    result = response.json()["results"][0]
    assert result["price_usd"] == "57.00"
    assert result["list_price_usd"] == "57.00"
    assert result["discount_percent"] == "0.00"
    assert result["pricing_reason"] == PricingReason.RETAIL


@pytest.mark.django_db
def test_public_price_dict_matches_resolve(
    package: Package, family_user, settings
) -> None:
    settings.PRICING_PROFILES_ENABLED = True
    _user, account, _profile = family_user
    quote = resolve_package_quote(package, account=account)
    pub = public_price_dict(quote)
    assert pub["price_usd"] == Decimal("54.15")
    assert pub["list_price_usd"] == Decimal("57.00")
    assert set(pub) == {
        "price_usd",
        "list_price_usd",
        "discount_percent",
        "pricing_reason",
    }


@pytest.mark.django_db
def test_preview_equals_pricing_service_resolve(
    package: Package, family_user, settings
) -> None:
    from django.utils import timezone

    settings.PRICING_PROFILES_ENABLED = True
    _user, account, _profile = family_user
    at = timezone.now()
    via_helper = resolve_package_quote(package, account=account, at=at)
    via_preview = resolve_preview_quote(
        list_price=package.price_usd,
        net_price=package.net_price_usd,
        order_type=OrderType.PACKAGE,
        account=account,
        at=at,
    )
    assert via_helper.customer_price == via_preview.customer_price
    assert via_helper.pricing_context_hash == via_preview.pricing_context_hash
    assert via_helper.fingerprint == via_preview.fingerprint


@pytest.mark.django_db
def test_internal_preview_staff_ok(
    client: Client, package: Package, family_user, settings
) -> None:
    settings.PRICING_PROFILES_ENABLED = True
    _user, account, _profile = family_user
    staff = User.objects.create_user(
        email="staff-preview@example.com", password=PASSWORD, is_staff=True
    )
    staff.is_active = True
    staff.save()
    response = client.post(
        "/api/internal/pricing/preview",
        data={
            "order_type": "package",
            "package_id": package.external_id,
            "account_id": str(account.pk),
        },
        content_type="application/json",
        **_auth_header(staff),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["price_usd"] == "54.15"
    assert body["list_price_usd"] == "57.00"
    assert body["pricing_reason"] == PricingReason.PRICING_PROFILE
    assert body["quote_fingerprint"]
    assert body["pricing_context_hash"]


@pytest.mark.django_db
def test_internal_preview_non_staff_forbidden(
    client: Client, package: Package, family_user
) -> None:
    user, _account, _profile = family_user
    response = client.post(
        "/api/internal/pricing/preview",
        data={"order_type": "package", "package_id": package.external_id},
        content_type="application/json",
        **_auth_header(user),
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_internal_preview_anonymous_forbidden(client: Client, package: Package) -> None:
    response = client.post(
        "/api/internal/pricing/preview",
        data={"order_type": "package", "package_id": package.external_id},
        content_type="application/json",
    )
    assert response.status_code in {401, 403}
