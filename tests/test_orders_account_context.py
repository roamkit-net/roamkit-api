"""PR13: Account context for team order spend (ADR 020)."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone

from apps.billing.models import LedgerReferenceType
from apps.billing.services import credit_service, ensure_billing_account
from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.orders.models import Order
from apps.organizations.models import (
    Membership,
    MembershipRole,
    MembershipStatus,
    OrganizationStatus,
)
from apps.organizations.services import create_organization, resolve_account_context
from shared.providers.esim import OrderedSimDTO, OrderResult

User = get_user_model()


class FakeOrderProvider:
    def __init__(self, result: OrderResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def create_order(self, package_id: str, customer_ref: str) -> OrderResult:
        self.calls.append((package_id, customer_ref))
        return self.result


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def owner(db) -> User:
    return User.objects.create_user(email="owner@example.com", password="SecurePass1!")


@pytest.fixture
def member_user(db) -> User:
    return User.objects.create_user(email="member@example.com", password="SecurePass1!")


@pytest.fixture
def viewer_user(db) -> User:
    return User.objects.create_user(email="viewer@example.com", password="SecurePass1!")


@pytest.fixture
def stranger(db) -> User:
    return User.objects.create_user(
        email="stranger@example.com", password="SecurePass1!"
    )


@pytest.fixture
def package(db) -> Package:
    return Package.objects.create(
        external_id="pkg-us-1gb-7d",
        title="1 GB - 7 Days",
        operator_title="Change",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("11.50"),
        synced_at=timezone.now(),
        is_active=True,
    )


@pytest.fixture
def order_result() -> OrderResult:
    return OrderResult(
        external_order_id="9666",
        code="20230227-009666",
        package_id="pkg-us-1gb-7d",
        customer_ref="rk",
        currency="USD",
        price_usd=Decimal("9.50"),
        manual_installation="<p>Manual</p>",
        qrcode_installation="<p>QR</p>",
        installation_guide_url="https://sandbox.airalo.com/installation-guide",
        sims=[
            OrderedSimDTO(
                iccid="891000000000009125",
                lpa="lpa.airalo.com",
                matching_id="TEST",
                qrcode="LPA:1$lpa.airalo.com$TEST",
                qrcode_url="https://sandbox.airalo.com/qr?id=1",
                direct_apple_installation_url=(
                    "https://esimsetup.apple.com/esim_qrcode_provisioning"
                    "?carddata=LPA:1$lpa.airalo.com$TEST"
                ),
            )
        ],
    )


@pytest.fixture
def org(owner):
    return create_organization(name="Fleet Ops", actor=owner)


def _add_member(org, user, role: str) -> Membership:
    return Membership.objects.create(
        organization=org,
        user=user,
        role=role,
        status=MembershipStatus.ACTIVE,
    )


def _auth_headers(client: Client, user: User) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": user.email, "password": "SecurePass1!"}),
        content_type="application/json",
    )
    assert response.status_code == 200
    return {"HTTP_AUTHORIZATION": f"Bearer {response.json()['access']}"}


def _fund_account(account, amount: str, key: str) -> None:
    credit_service.credit(
        account,
        Decimal(amount),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id=key,
        idempotency_key=key,
    )


def _post_order(
    client: Client,
    user: User,
    *,
    package: Package,
    idempotency_key: str,
    organization_id=None,
    extra: dict | None = None,
):
    body: dict = {
        "package_id": package.external_id,
        "idempotency_key": idempotency_key,
    }
    if organization_id is not None:
        body["organization_id"] = str(organization_id)
    if extra:
        body.update(extra)
    return client.post(
        "/api/v1/orders/",
        data=json.dumps(body),
        content_type="application/json",
        **_auth_headers(client, user),
    )


@pytest.mark.django_db
def test_resolve_account_context_personal_vs_org(owner, org):
    personal = resolve_account_context(owner)
    assert personal.kind == "personal"
    assert personal.account == ensure_billing_account(owner)

    team = resolve_account_context(owner, organization_id=org.pk)
    assert team.kind == "organization"
    assert team.account.pk == org.account.pk


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_personal_order_unchanged_without_organization_id(
    client, owner, package, order_result, monkeypatch
):
    personal = ensure_billing_account(owner)
    _fund_account(personal, "20.00", f"fund-personal:{owner.pk}")
    monkeypatch.setattr(
        "apps.orders.views.get_order_provider",
        lambda: FakeOrderProvider(order_result),
    )

    response = _post_order(
        client, owner, package=package, idempotency_key="personal-order-1"
    )
    assert response.status_code == 201

    order = Order.objects.get(idempotency_key="personal-order-1")
    assert order.account_id == personal.pk
    personal.refresh_from_db()
    assert personal.balance == Decimal("8.50")

    esim = Esim.objects.get(order=order)
    assert esim.account_id == personal.pk
    assert esim.user_id == owner.pk


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_member_team_order_debits_team_account(
    client, owner, member_user, org, package, order_result, monkeypatch
):
    _add_member(org, member_user, MembershipRole.MEMBER)
    personal = ensure_billing_account(member_user)
    _fund_account(personal, "100.00", f"fund-personal:{member_user.pk}")
    _fund_account(org.account, "20.00", f"fund-team:{org.pk}")
    monkeypatch.setattr(
        "apps.orders.views.get_order_provider",
        lambda: FakeOrderProvider(order_result),
    )

    response = _post_order(
        client,
        member_user,
        package=package,
        idempotency_key="team-order-1",
        organization_id=org.pk,
    )
    assert response.status_code == 201, response.content

    order = Order.objects.get(idempotency_key="team-order-1")
    assert order.account_id == org.account_id

    org.account.refresh_from_db()
    personal.refresh_from_db()
    assert org.account.balance == Decimal("8.50")
    assert personal.balance == Decimal("100.00")

    esim = Esim.objects.get(order=order)
    assert esim.account_id == org.account_id
    assert esim.user_id == member_user.pk


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_viewer_team_order_forbidden(
    client, owner, viewer_user, org, package, order_result, monkeypatch
):
    _add_member(org, viewer_user, MembershipRole.VIEWER)
    _fund_account(org.account, "20.00", f"fund-team:{org.pk}")
    monkeypatch.setattr(
        "apps.orders.views.get_order_provider",
        lambda: FakeOrderProvider(order_result),
    )

    response = _post_order(
        client,
        viewer_user,
        package=package,
        idempotency_key="viewer-order-1",
        organization_id=org.pk,
    )
    assert response.status_code == 403
    assert Order.objects.filter(idempotency_key="viewer-order-1").count() == 0


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_foreign_organization_id_not_found(
    client, owner, stranger, package, order_result, monkeypatch
):
    org_b = create_organization(name="Other Org", actor=stranger)
    _fund_account(org_b.account, "20.00", f"fund-team:{org_b.pk}")
    monkeypatch.setattr(
        "apps.orders.views.get_order_provider",
        lambda: FakeOrderProvider(order_result),
    )

    response = _post_order(
        client,
        owner,
        package=package,
        idempotency_key="cross-org-1",
        organization_id=org_b.pk,
    )
    assert response.status_code == 404


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_unknown_organization_id_not_found(
    client, owner, package, order_result, monkeypatch
):
    monkeypatch.setattr(
        "apps.orders.views.get_order_provider",
        lambda: FakeOrderProvider(order_result),
    )
    response = _post_order(
        client,
        owner,
        package=package,
        idempotency_key="unknown-org-1",
        organization_id=uuid.uuid4(),
    )
    assert response.status_code == 404


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_suspended_membership_cannot_team_spend(
    client, owner, member_user, org, package, order_result, monkeypatch
):
    membership = _add_member(org, member_user, MembershipRole.MEMBER)
    membership.status = MembershipStatus.SUSPENDED
    membership.save(update_fields=["status", "updated_at"])
    _fund_account(org.account, "20.00", f"fund-team:{org.pk}")
    monkeypatch.setattr(
        "apps.orders.views.get_order_provider",
        lambda: FakeOrderProvider(order_result),
    )

    response = _post_order(
        client,
        member_user,
        package=package,
        idempotency_key="suspended-member-1",
        organization_id=org.pk,
    )
    assert response.status_code == 403


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_suspended_org_cannot_team_spend(
    client, owner, org, package, order_result, monkeypatch
):
    org.status = OrganizationStatus.SUSPENDED
    org.save(update_fields=["status", "updated_at"])
    _fund_account(org.account, "20.00", f"fund-team:{org.pk}")
    monkeypatch.setattr(
        "apps.orders.views.get_order_provider",
        lambda: FakeOrderProvider(order_result),
    )

    response = _post_order(
        client,
        owner,
        package=package,
        idempotency_key="suspended-org-1",
        organization_id=org.pk,
    )
    assert response.status_code == 403


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_archived_org_cannot_team_spend(
    client, owner, org, package, order_result, monkeypatch
):
    org.status = OrganizationStatus.ARCHIVED
    org.save(update_fields=["status", "updated_at"])
    _fund_account(org.account, "20.00", f"fund-team:{org.pk}")
    monkeypatch.setattr(
        "apps.orders.views.get_order_provider",
        lambda: FakeOrderProvider(order_result),
    )

    response = _post_order(
        client,
        owner,
        package=package,
        idempotency_key="archived-org-1",
        organization_id=org.pk,
    )
    assert response.status_code == 403


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_client_account_id_rejected(
    client, owner, stranger, org, package, order_result, monkeypatch
):
    """Forged account_id must not authorize spend against another Account."""
    org_b = create_organization(name="Victim Org", actor=stranger)
    _fund_account(org_b.account, "20.00", f"fund-victim:{org_b.pk}")
    monkeypatch.setattr(
        "apps.orders.views.get_order_provider",
        lambda: FakeOrderProvider(order_result),
    )

    response = _post_order(
        client,
        owner,
        package=package,
        idempotency_key="forged-account-1",
        organization_id=org.pk,
        extra={"account_id": str(org_b.account_id)},
    )
    assert response.status_code == 400
    assert "account_id" in response.json()
    assert Order.objects.filter(idempotency_key="forged-account-1").count() == 0
    org_b.account.refresh_from_db()
    assert org_b.account.balance == Decimal("20.00")
