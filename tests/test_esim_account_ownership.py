"""Esim.account inventory ownership (ADR 020 / PR4)."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.utils import timezone

from apps.billing.models import AccountKind
from apps.billing.services import ensure_billing_account
from apps.catalog.models import Location, Package
from apps.esims.models import Esim
from apps.esims.services.lifecycle_service import lifecycle_service
from apps.orders.models import Order
from apps.orders.product_snapshot import product_snapshot_kwargs
from apps.organizations.services import create_organization

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email="esim-own@example.com",
        password="SecurePass1!",
    )


@pytest.fixture
def other(db):
    return User.objects.create_user(
        email="esim-other@example.com", password="SecurePass1!"
    )


@pytest.fixture
def package(db) -> Package:
    location = Location.objects.create(
        slug="montenegro",
        title="Montenegro",
        country_code="ME",
        coverage_type=Location.COVERAGE_LOCAL,
    )
    return Package.objects.create(
        external_id="jezero-1gb",
        title="1 GB",
        operator_title="Jezero",
        country_code="ME",
        location=location,
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("5.00"),
        net_price_usd=Decimal("2.00"),
        synced_at=timezone.now(),
    )


@pytest.fixture
def order(user, package) -> Order:
    account = ensure_billing_account(user)
    return Order.objects.create(
        account=account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id="ext-1",
        customer_ref="rk-1",
        **product_snapshot_kwargs(package),
    )


@pytest.mark.django_db
def test_create_purchased_sets_account_and_user(user, order):
    account = ensure_billing_account(user)
    esim = lifecycle_service.create_purchased(
        user=user,
        account=account,
        order=order,
        iccid="8910300000000000001",
    )
    assert esim.account_id == account.pk
    assert esim.user_id == user.pk
    assert esim.assigned_user_id is None
    assert account.kind == AccountKind.PERSONAL


@pytest.mark.django_db
def test_every_esim_has_exactly_one_account_owner(user, order):
    account = ensure_billing_account(user)
    for i in range(3):
        lifecycle_service.create_purchased(
            user=user,
            account=account,
            order=order,
            iccid=f"89103000000000000{i}2",
        )
    esims = list(Esim.objects.all())
    assert esims
    for esim in esims:
        assert esim.account_id is not None
        assert Esim.objects.filter(pk=esim.pk, account_id=esim.account_id).count() == 1


@pytest.mark.django_db
def test_assigned_user_is_not_inventory_owner(user, other, order):
    account = ensure_billing_account(user)
    esim = lifecycle_service.create_purchased(
        user=user,
        account=account,
        order=order,
        iccid="8910300000000000099",
    )
    esim.assigned_user = other
    esim.save(update_fields=["assigned_user", "updated_at"])

    client = Client()
    token = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": other.email, "password": "SecurePass1!"}),
        content_type="application/json",
    ).json()["access"]
    resp = client.get(
        f"/api/v1/me/esims/{esim.pk}/",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert resp.status_code == 404

    owner_token = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": user.email, "password": "SecurePass1!"}),
        content_type="application/json",
    ).json()["access"]
    resp_owner = client.get(
        f"/api/v1/me/esims/{esim.pk}/",
        HTTP_AUTHORIZATION=f"Bearer {owner_token}",
    )
    assert resp_owner.status_code == 200


@pytest.mark.django_db
def test_backfill_maps_to_personal_account_not_org_account(user, order):
    """Migration must not attach team Accounts."""
    personal = ensure_billing_account(user)
    org = create_organization(name="Fleet")
    assert org.account_id != personal.pk

    esim = lifecycle_service.create_purchased(
        user=user,
        account=personal,
        order=order,
        iccid="8910300000000000088",
    )
    assert esim.account_id == personal.pk
    assert esim.account_id != org.account_id


@pytest.mark.django_db(transaction=True)
def test_esim_account_migration_backfill_preserves_owner() -> None:
    """0010 backfill attaches personal Account for each Esim.user."""
    executor = MigrationExecutor(connection)
    executor.migrate(
        [
            ("organizations", "0001_organization_membership_schema"),
            ("billing", "0004_account_pricing_profile"),
            ("esims", "0009_esim_archived_at"),
            ("orders", "0005_order_pricing_snapshot"),
            ("catalog", "0005_package_activation_policy"),
            ("accounts", "0002_billing_schema"),
        ]
    )
    executor.loader.build_graph()

    state = executor.loader.project_state(
        [
            ("accounts", "0002_billing_schema"),
            ("billing", "0004_account_pricing_profile"),
            ("esims", "0009_esim_archived_at"),
            ("orders", "0005_order_pricing_snapshot"),
            ("catalog", "0005_package_activation_policy"),
        ]
    )
    UserH = state.apps.get_model("accounts", "User")
    AccountH = state.apps.get_model("billing", "Account")
    PackageH = state.apps.get_model("catalog", "Package")
    LocationH = state.apps.get_model("catalog", "Location")
    OrderH = state.apps.get_model("orders", "Order")
    EsimH = state.apps.get_model("esims", "Esim")

    user = UserH.objects.create(
        email="esim-mig@example.com",
        password="!",
        is_active=True,
        is_staff=False,
        is_superuser=False,
    )
    account = AccountH.objects.create(
        id=uuid.uuid4(),
        user_id=user.pk,
        balance=Decimal("0"),
        version=0,
    )
    location = LocationH.objects.create(
        slug="mig-me",
        title="Montenegro",
        country_code="ME",
        coverage_type="local",
        image_url="",
        covered_country_codes=[],
        coverages=[],
        is_popular=False,
    )
    package = PackageH.objects.create(
        external_id="pkg-esim-mig",
        title="1 GB",
        operator_title="Jezero",
        country_code="ME",
        location_id=location.pk,
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("5.00"),
        net_price_usd=Decimal("2.00"),
        is_unlimited=False,
        plan_type="data",
        source="airalo",
        is_active=True,
        synced_at=timezone.now(),
        activation_policy="first_usage",
    )
    order = OrderH.objects.create(
        account_id=account.pk,
        package_id=package.pk,
        status="fulfilled",
        external_order_id="mig-1",
        customer_ref="rk-mig",
    )
    esim = EsimH.objects.create(
        user_id=user.pk,
        order_id=order.pk,
        iccid="8910300000000000777",
        status="purchased",
        activation_policy="unknown",
        note="",
    )

    executor.migrate([("esims", "0010_esim_account_ownership")])
    executor.loader.build_graph()

    EsimAfter = executor.loader.project_state(
        [("esims", "0010_esim_account_ownership")]
    ).apps.get_model("esims", "Esim")
    migrated = EsimAfter.objects.get(pk=esim.pk)
    assert migrated.account_id == account.pk
    assert migrated.user_id == user.pk
    assert migrated.assigned_user_id is None

    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())
