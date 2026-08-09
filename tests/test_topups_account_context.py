"""PR14: top-up Account context + Esim.account ownership (ADR 020)."""

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
from apps.esims.models import Esim, Topup
from apps.orders.models import Order
from apps.organizations.models import (
    Membership,
    MembershipRole,
    MembershipStatus,
    OrganizationStatus,
)
from apps.organizations.services import create_organization
from shared.providers.esim import TopupPackage, TopupResult, UsageDTO

User = get_user_model()


class FakeTopupProvider:
    def __init__(self, *, result: TopupResult | None = None) -> None:
        self.result = result
        self.topups = [
            TopupPackage(
                external_id="topup-1gb",
                title="1 GB Top-up",
                data_allowance="1 GB",
                validity_days=7,
                price_usd=Decimal("5.00"),
                net_price_usd=Decimal("4.50"),
                is_unlimited=False,
                plan_type="topup",
            )
        ]

    def list_topups(self, iccid: str) -> list[TopupPackage]:
        return self.topups

    def submit_topup(self, iccid: str, package_id: str) -> TopupResult:
        assert self.result is not None
        return self.result

    def get_usage(self, iccid: str) -> UsageDTO:
        raise AssertionError("unused")


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


def _make_esim(
    *,
    account,
    user,
    package: Package,
    iccid: str = "891000000000009125",
) -> Esim:
    order = Order.objects.create(
        account=account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id=f"ext-{iccid[-4:]}",
        customer_ref=f"ref-{iccid[-4:]}",
    )
    return Esim.objects.create(
        user=user,
        account=account,
        order=order,
        iccid=iccid,
        status=Esim.Status.ACTIVATED,
    )


def _topup_result(esim: Esim) -> TopupResult:
    return TopupResult(
        external_order_id="top-1",
        code="T1",
        package_id="topup-1gb",
        iccid=esim.iccid,
        currency="USD",
        price_usd=Decimal("5.00"),
        customer_ref="ref",
    )


def _post_topup(
    client: Client,
    user: User,
    esim: Esim,
    *,
    idempotency_key: str,
    organization_id=None,
    extra: dict | None = None,
):
    body: dict = {
        "package_id": "topup-1gb",
        "idempotency_key": idempotency_key,
    }
    if organization_id is not None:
        body["organization_id"] = str(organization_id)
    if extra:
        body.update(extra)
    return client.post(
        f"/api/v1/me/esims/{esim.pk}/topups/",
        data=json.dumps(body),
        content_type="application/json",
        **_auth_headers(client, user),
    )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_personal_topup_without_organization_id(client, owner, package, monkeypatch):
    personal = ensure_billing_account(owner)
    _fund_account(personal, "10.00", f"fund-personal:{owner.pk}")
    esim = _make_esim(account=personal, user=owner, package=package)
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider",
        lambda: FakeTopupProvider(result=_topup_result(esim)),
    )

    response = _post_topup(client, owner, esim, idempotency_key="personal-topup-1")
    assert response.status_code == 201, response.content

    topup = Topup.objects.get(idempotency_key="personal-topup-1")
    assert topup.account_id == personal.pk
    personal.refresh_from_db()
    assert personal.balance == Decimal("5.000000")


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_team_topup_debits_team_account_not_actor_personal(
    client, owner, member_user, org, package, monkeypatch
):
    _add_member(org, member_user, MembershipRole.MEMBER)
    # Team eSIM dual-writes purchasing actor as user; ownership is account.
    esim = _make_esim(account=org.account, user=owner, package=package)
    personal = ensure_billing_account(member_user)
    _fund_account(personal, "100.00", f"fund-personal:{member_user.pk}")
    _fund_account(org.account, "10.00", f"fund-team:{org.pk}")
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider",
        lambda: FakeTopupProvider(result=_topup_result(esim)),
    )

    response = _post_topup(
        client,
        member_user,
        esim,
        idempotency_key="team-topup-1",
        organization_id=org.pk,
    )
    assert response.status_code == 201, response.content

    topup = Topup.objects.get(idempotency_key="team-topup-1")
    assert topup.account_id == org.account_id
    org.account.refresh_from_db()
    personal.refresh_from_db()
    assert org.account.balance == Decimal("5.000000")
    assert personal.balance == Decimal("100.00")


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_esim_user_dual_write_does_not_authorize_personal_topup_on_team_esim(
    client, owner, org, package, monkeypatch
):
    """Team eSIM with user=owner must not top up via personal context."""
    personal = ensure_billing_account(owner)
    _fund_account(personal, "10.00", f"fund-personal:{owner.pk}")
    _fund_account(org.account, "10.00", f"fund-team:{org.pk}")
    esim = _make_esim(account=org.account, user=owner, package=package)
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider",
        lambda: FakeTopupProvider(result=_topup_result(esim)),
    )

    response = _post_topup(client, owner, esim, idempotency_key="dual-write-reject-1")
    assert response.status_code == 404
    assert Topup.objects.filter(idempotency_key="dual-write-reject-1").count() == 0
    personal.refresh_from_db()
    org.account.refresh_from_db()
    assert personal.balance == Decimal("10.00")
    assert org.account.balance == Decimal("10.00")


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_foreign_account_esim_not_found(client, owner, stranger, package, monkeypatch):
    org_b = create_organization(name="Other", actor=stranger)
    esim = _make_esim(
        account=org_b.account,
        user=stranger,
        package=package,
        iccid="891000000000009999",
    )
    _fund_account(org_b.account, "10.00", f"fund-other:{org_b.pk}")
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider",
        lambda: FakeTopupProvider(result=_topup_result(esim)),
    )

    org_a = create_organization(name="Mine", actor=owner)
    _fund_account(org_a.account, "10.00", f"fund-mine:{org_a.pk}")

    response = _post_topup(
        client,
        owner,
        esim,
        idempotency_key="cross-account-1",
        organization_id=org_a.pk,
    )
    assert response.status_code == 404
    assert "iccid" not in response.content.decode().lower()
    org_b.account.refresh_from_db()
    assert org_b.account.balance == Decimal("10.00")


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_viewer_team_topup_forbidden(
    client, owner, viewer_user, org, package, monkeypatch
):
    _add_member(org, viewer_user, MembershipRole.VIEWER)
    esim = _make_esim(account=org.account, user=owner, package=package)
    _fund_account(org.account, "10.00", f"fund-team:{org.pk}")
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider",
        lambda: FakeTopupProvider(result=_topup_result(esim)),
    )

    response = _post_topup(
        client,
        viewer_user,
        esim,
        idempotency_key="viewer-topup-1",
        organization_id=org.pk,
    )
    assert response.status_code == 403


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_suspended_membership_cannot_topup(
    client, owner, member_user, org, package, monkeypatch
):
    membership = _add_member(org, member_user, MembershipRole.MEMBER)
    membership.status = MembershipStatus.SUSPENDED
    membership.save(update_fields=["status", "updated_at"])
    esim = _make_esim(account=org.account, user=owner, package=package)
    _fund_account(org.account, "10.00", f"fund-team:{org.pk}")
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider",
        lambda: FakeTopupProvider(result=_topup_result(esim)),
    )

    response = _post_topup(
        client,
        member_user,
        esim,
        idempotency_key="suspended-member-topup-1",
        organization_id=org.pk,
    )
    assert response.status_code == 403


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_revoked_membership_cannot_topup(
    client, owner, member_user, org, package, monkeypatch
):
    membership = _add_member(org, member_user, MembershipRole.MEMBER)
    membership.status = MembershipStatus.REVOKED
    membership.save(update_fields=["status", "updated_at"])
    esim = _make_esim(account=org.account, user=owner, package=package)
    _fund_account(org.account, "10.00", f"fund-team:{org.pk}")
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider",
        lambda: FakeTopupProvider(result=_topup_result(esim)),
    )

    response = _post_topup(
        client,
        member_user,
        esim,
        idempotency_key="revoked-member-topup-1",
        organization_id=org.pk,
    )
    assert response.status_code == 403


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_suspended_org_cannot_topup(client, owner, org, package, monkeypatch):
    org.status = OrganizationStatus.SUSPENDED
    org.save(update_fields=["status", "updated_at"])
    esim = _make_esim(account=org.account, user=owner, package=package)
    _fund_account(org.account, "10.00", f"fund-team:{org.pk}")
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider",
        lambda: FakeTopupProvider(result=_topup_result(esim)),
    )

    response = _post_topup(
        client,
        owner,
        esim,
        idempotency_key="suspended-org-topup-1",
        organization_id=org.pk,
    )
    assert response.status_code == 403


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_archived_org_cannot_topup(client, owner, org, package, monkeypatch):
    org.status = OrganizationStatus.ARCHIVED
    org.save(update_fields=["status", "updated_at"])
    esim = _make_esim(account=org.account, user=owner, package=package)
    _fund_account(org.account, "10.00", f"fund-team:{org.pk}")
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider",
        lambda: FakeTopupProvider(result=_topup_result(esim)),
    )

    response = _post_topup(
        client,
        owner,
        esim,
        idempotency_key="archived-org-topup-1",
        organization_id=org.pk,
    )
    assert response.status_code == 403


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_client_account_id_rejected(client, owner, stranger, org, package, monkeypatch):
    org_b = create_organization(name="Victim", actor=stranger)
    esim = _make_esim(account=org.account, user=owner, package=package)
    _fund_account(org.account, "10.00", f"fund-team:{org.pk}")
    _fund_account(org_b.account, "50.00", f"fund-victim:{org_b.pk}")
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider",
        lambda: FakeTopupProvider(result=_topup_result(esim)),
    )

    response = _post_topup(
        client,
        owner,
        esim,
        idempotency_key="forged-account-topup-1",
        organization_id=org.pk,
        extra={"account_id": str(org_b.account_id)},
    )
    assert response.status_code == 400
    assert "account_id" in response.json()
    assert Topup.objects.filter(idempotency_key="forged-account-topup-1").count() == 0
    org_b.account.refresh_from_db()
    assert org_b.account.balance == Decimal("50.00")


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_unknown_organization_id_not_found(client, owner, package, monkeypatch):
    personal = ensure_billing_account(owner)
    esim = _make_esim(account=personal, user=owner, package=package)
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider",
        lambda: FakeTopupProvider(result=_topup_result(esim)),
    )
    response = _post_topup(
        client,
        owner,
        esim,
        idempotency_key="unknown-org-topup-1",
        organization_id=uuid.uuid4(),
    )
    assert response.status_code == 404
