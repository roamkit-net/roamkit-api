"""PR15: Account-scoped inventory authz cutover (ADR 020)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone

from apps.billing.services import ensure_billing_account
from apps.catalog.models import Package
from apps.esims.models import Esim, EsimAutoTopupPolicy
from apps.orders.models import Order
from apps.organizations.models import (
    Membership,
    MembershipRole,
    MembershipStatus,
    OrganizationStatus,
)
from apps.organizations.services import create_organization
from shared.providers.esim import TopupPackage, UsageDTO

User = get_user_model()

REPO_ROOT = Path(__file__).resolve().parents[1]
VIEWS_PATH = REPO_ROOT / "src" / "apps" / "esims" / "views.py"


class FakeUsageProvider:
    def list_topups(self, iccid: str):
        return [
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

    def submit_topup(self, iccid: str, package_id: str):
        raise AssertionError("unused")

    def get_usage(self, iccid: str) -> UsageDTO:
        return UsageDTO(
            remaining_mb=100,
            total_mb=1024,
            expired_at=None,
            is_unlimited=False,
            status="ACTIVE",
            remaining_voice=0,
            remaining_text=0,
            total_voice=0,
            total_text=0,
        )


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


def _q(organization_id=None) -> str:
    if organization_id is None:
        return ""
    return f"?organization_id={organization_id}"


@pytest.mark.django_db
def test_no_user_q_fallback_in_owned_esim_mixin():
    source = VIEWS_PATH.read_text(encoding="utf-8")
    assert "Q(user=" not in source
    assert "Q(account=account) | Q(user=" not in source
    assert "Esim.objects.filter(account=context.account)" in source


@pytest.mark.django_db
def test_personal_list_detail_usage_events_archive_compatible(
    client, owner, package, monkeypatch
):
    personal = ensure_billing_account(owner)
    esim = _make_esim(account=personal, user=owner, package=package)
    headers = _auth_headers(client, owner)
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider", lambda: FakeUsageProvider()
    )

    listed = client.get("/api/v1/me/esims/", **headers)
    assert listed.status_code == 200
    assert any(row["id"] == esim.pk for row in listed.json()["results"])

    detail = client.get(f"/api/v1/me/esims/{esim.pk}/", **headers)
    assert detail.status_code == 200

    usage = client.get(f"/api/v1/me/esims/{esim.pk}/usage/", **headers)
    assert usage.status_code == 200

    events = client.get(f"/api/v1/me/esims/{esim.pk}/events/", **headers)
    assert events.status_code == 200

    archived = client.post(f"/api/v1/me/esims/{esim.pk}/archive/", **headers)
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None

    unarchived = client.post(f"/api/v1/me/esims/{esim.pk}/unarchive/", **headers)
    assert unarchived.status_code == 200
    assert unarchived.json()["archived_at"] is None


@pytest.mark.django_db
def test_team_member_sees_team_esim_regardless_of_esim_user(
    client, owner, member_user, org, package, monkeypatch
):
    _add_member(org, member_user, MembershipRole.MEMBER)
    # Dual-write purchaser is owner; member must still see via organization_id.
    esim = _make_esim(account=org.account, user=owner, package=package)
    headers = _auth_headers(client, member_user)
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider", lambda: FakeUsageProvider()
    )
    q = _q(org.pk)

    listed = client.get(f"/api/v1/me/esims/{q}", **headers)
    assert listed.status_code == 200
    assert any(row["id"] == esim.pk for row in listed.json()["results"])

    detail = client.get(f"/api/v1/me/esims/{esim.pk}/{q}", **headers)
    assert detail.status_code == 200

    usage = client.get(f"/api/v1/me/esims/{esim.pk}/usage/{q}", **headers)
    assert usage.status_code == 200

    events = client.get(f"/api/v1/me/esims/{esim.pk}/events/{q}", **headers)
    assert events.status_code == 200


@pytest.mark.django_db
def test_viewer_can_view_team_inventory_but_not_mutate(
    client, owner, viewer_user, org, package
):
    _add_member(org, viewer_user, MembershipRole.VIEWER)
    esim = _make_esim(account=org.account, user=owner, package=package)
    headers = _auth_headers(client, viewer_user)
    q = _q(org.pk)

    assert client.get(f"/api/v1/me/esims/{q}", **headers).status_code == 200
    assert client.get(f"/api/v1/me/esims/{esim.pk}/{q}", **headers).status_code == 200
    assert (
        client.post(f"/api/v1/me/esims/{esim.pk}/archive/{q}", **headers).status_code
        == 403
    )


@pytest.mark.django_db
def test_esim_user_dual_write_does_not_authorize_wrong_account(
    client, owner, org, package
):
    personal = ensure_billing_account(owner)
    esim = _make_esim(account=org.account, user=owner, package=package)
    headers = _auth_headers(client, owner)

    # Personal context: team eSIM must not appear via Esim.user dual-write.
    listed = client.get("/api/v1/me/esims/", **headers)
    assert listed.status_code == 200
    assert all(row["id"] != esim.pk for row in listed.json()["results"])

    assert client.get(f"/api/v1/me/esims/{esim.pk}/", **headers).status_code == 404

    # Explicit wrong personal ownership: forge would be account mismatch.
    personal_esim = _make_esim(
        account=personal,
        user=owner,
        package=package,
        iccid="891000000000009999",
    )
    assert (
        client.get(f"/api/v1/me/esims/{personal_esim.pk}/", **headers).status_code
        == 200
    )


@pytest.mark.django_db
def test_foreign_org_esim_not_found(client, owner, stranger, package):
    org_a = create_organization(name="Mine", actor=owner)
    org_b = create_organization(name="Theirs", actor=stranger)
    esim = _make_esim(
        account=org_b.account,
        user=stranger,
        package=package,
        iccid="891000000000008888",
    )
    headers = _auth_headers(client, owner)
    q = _q(org_a.pk)
    assert client.get(f"/api/v1/me/esims/{esim.pk}/{q}", **headers).status_code == 404


@pytest.mark.django_db
def test_suspended_membership_no_team_access(client, owner, member_user, org, package):
    membership = _add_member(org, member_user, MembershipRole.MEMBER)
    membership.status = MembershipStatus.SUSPENDED
    membership.save(update_fields=["status", "updated_at"])
    esim = _make_esim(account=org.account, user=owner, package=package)
    headers = _auth_headers(client, member_user)
    q = _q(org.pk)
    assert client.get(f"/api/v1/me/esims/{q}", **headers).status_code == 403
    assert client.get(f"/api/v1/me/esims/{esim.pk}/{q}", **headers).status_code == 403


@pytest.mark.django_db
def test_revoked_membership_no_team_access(client, owner, member_user, org, package):
    membership = _add_member(org, member_user, MembershipRole.MEMBER)
    membership.status = MembershipStatus.REVOKED
    membership.save(update_fields=["status", "updated_at"])
    headers = _auth_headers(client, member_user)
    assert client.get(f"/api/v1/me/esims/{_q(org.pk)}", **headers).status_code == 403


@pytest.mark.django_db
def test_suspended_org_allows_read_blocks_mutation(client, owner, org, package):
    esim = _make_esim(account=org.account, user=owner, package=package)
    org.status = OrganizationStatus.SUSPENDED
    org.save(update_fields=["status", "updated_at"])
    headers = _auth_headers(client, owner)
    q = _q(org.pk)

    assert client.get(f"/api/v1/me/esims/{q}", **headers).status_code == 200
    assert client.get(f"/api/v1/me/esims/{esim.pk}/{q}", **headers).status_code == 200
    assert (
        client.post(f"/api/v1/me/esims/{esim.pk}/archive/{q}", **headers).status_code
        == 403
    )


@pytest.mark.django_db
def test_archived_org_allows_read_blocks_mutation(client, owner, org, package):
    esim = _make_esim(account=org.account, user=owner, package=package)
    org.status = OrganizationStatus.ARCHIVED
    org.save(update_fields=["status", "updated_at"])
    headers = _auth_headers(client, owner)
    q = _q(org.pk)

    assert client.get(f"/api/v1/me/esims/{esim.pk}/{q}", **headers).status_code == 200
    assert (
        client.patch(
            f"/api/v1/me/esims/{esim.pk}/{q}",
            data=json.dumps({"note": "x"}),
            content_type="application/json",
            **headers,
        ).status_code
        == 403
    )


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=True,
    AUTO_TOPUP_ROLLOUT_MODE="all",
)
def test_auto_topup_policy_uses_account_ownership_gate(
    client, owner, member_user, org, package, monkeypatch
):
    _add_member(org, member_user, MembershipRole.MEMBER)
    esim = _make_esim(account=org.account, user=owner, package=package)
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider", lambda: FakeUsageProvider()
    )
    headers = _auth_headers(client, member_user)
    q = _q(org.pk)

    # Dual-write user=owner must not open personal auto-topup path.
    personal_headers = _auth_headers(client, owner)
    assert (
        client.get(
            f"/api/v1/me/esims/{esim.pk}/auto-topup/", **personal_headers
        ).status_code
        == 404
    )

    create = client.put(
        f"/api/v1/me/esims/{esim.pk}/auto-topup/{q}",
        data=json.dumps(
            {
                "package_id": "topup-1gb",
                "enabled": True,
                "expiry_enabled": False,
                "usage_mode": "threshold",
                "threshold_mb": 500,
                "renew_mode": "until_funds",
            }
        ),
        content_type="application/json",
        **headers,
    )
    assert create.status_code == 201, create.content
    policy = EsimAutoTopupPolicy.objects.get(esim=esim)
    assert policy.account_id == org.account_id

    got = client.get(f"/api/v1/me/esims/{esim.pk}/auto-topup/{q}", **headers)
    assert got.status_code == 200


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=True,
    AUTO_TOPUP_ROLLOUT_MODE="all",
)
def test_viewer_cannot_write_auto_topup_policy(
    client, owner, viewer_user, org, package, monkeypatch
):
    _add_member(org, viewer_user, MembershipRole.VIEWER)
    esim = _make_esim(account=org.account, user=owner, package=package)
    monkeypatch.setattr(
        "apps.esims.views.get_topup_provider", lambda: FakeUsageProvider()
    )
    headers = _auth_headers(client, viewer_user)
    q = _q(org.pk)

    assert (
        client.get(f"/api/v1/me/esims/{esim.pk}/auto-topup/{q}", **headers).status_code
        == 404
    )
    create = client.put(
        f"/api/v1/me/esims/{esim.pk}/auto-topup/{q}",
        data=json.dumps(
            {
                "package_id": "topup-1gb",
                "enabled": True,
                "expiry_enabled": False,
                "usage_mode": "disabled",
                "renew_mode": "until_funds",
            }
        ),
        content_type="application/json",
        **headers,
    )
    assert create.status_code == 403
