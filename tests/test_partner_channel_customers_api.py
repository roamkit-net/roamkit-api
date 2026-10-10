"""GET /api/v1/partner/channels/{channel_id}/customers/ (ADR 024)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.billing.models import Account, CreditLedgerEntry, LedgerReferenceType
from apps.billing.partner_channel import (
    CustomerAttribution,
    CustomerAttributionHistory,
    PartnerChannel,
    PartnerMarginAccrual,
    PendingPartnerAttribution,
)
from apps.billing.services.credit import credit_service
from apps.organizations.models import Membership, MembershipRole, MembershipStatus
from apps.organizations.services.account_binding import create_organization

User = get_user_model()
PASSWORD = "SecurePass1!"
LEGACY_URL = "/api/v1/orgs/partner/customers/"
ENABLED = override_settings(PARTNER_CHANNEL_ENABLED=True, BILLING_ENABLED=True)
_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def client() -> Client:
    return Client()


def _user(prefix: str, *, display_name: str = "") -> User:
    user = User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4()}@example.com",
        password=PASSWORD,
    )
    if display_name:
        user.display_name = display_name
        user.save(update_fields=["display_name"])
    return user


def _auth(user: User) -> dict[str, str]:
    access = str(RefreshToken.for_user(user).access_token)
    return {"HTTP_AUTHORIZATION": f"Bearer {access}"}


def _url(channel_id: uuid.UUID | str) -> str:
    return f"/api/v1/partner/channels/{channel_id}/customers/"


def _get(client: Client, user: User | None, channel_id: uuid.UUID | str, **params: str):
    headers = _auth(user) if user is not None else {}
    return client.get(_url(channel_id), params, **headers)


def _legacy(client: Client, user: User, **params: str):
    return client.get(LEGACY_URL, params, **_auth(user))


def _individual(owner: User, *, active: bool = True) -> PartnerChannel:
    return PartnerChannel.objects.create(
        kind=PartnerChannel.Kind.INDIVIDUAL,
        owner_user=owner,
        organization=None,
        revenue_share_percent=Decimal("25.00"),
        is_active=active,
    )


def _team(actor: User, name: str, *, active: bool = True) -> PartnerChannel:
    org = create_organization(name=name, actor=actor)
    return PartnerChannel.objects.create(
        organization=org,
        revenue_share_percent=Decimal("50.00"),
        is_active=active,
    )


def _membership(
    *,
    user: User,
    channel: PartnerChannel,
    role: str,
    status: str = MembershipStatus.ACTIVE,
) -> None:
    Membership.objects.create(
        organization=channel.organization,
        user=user,
        role=role,
        status=status,
    )


def _attribute(customer: User, channel: PartnerChannel) -> CustomerAttribution:
    return CustomerAttribution.objects.create(
        user=customer,
        partner_channel=channel,
        source=CustomerAttribution.Source.ADMIN,
        attributed_at=_AT,
    )


def _accrual(channel: PartnerChannel, account, customer: User, share: str) -> None:
    entry = credit_service.credit(
        account,
        Decimal("0.250000"),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id=f"cust-{uuid.uuid4()}",
        idempotency_key=f"cust-{uuid.uuid4()}",
    )
    PartnerMarginAccrual.objects.create(
        partner_channel=channel,
        customer_user_id_snapshot=customer.pk,
        source_type=PartnerMarginAccrual.SourceType.ORDER,
        source_id=f"order-{uuid.uuid4()}",
        list_price=Decimal("10.000000"),
        net_price=Decimal("4.000000"),
        margin=Decimal("6.000000"),
        revenue_share_percent=Decimal("10.00"),
        partner_share=Decimal(share),
        ledger_entry=entry,
    )


def _ids(response) -> list[int]:
    return [row["customer_id"] for row in response.json()["results"]]


def _snapshot() -> tuple:
    return (
        list(Account.objects.order_by("id").values_list("id", "balance", "kind")),
        CreditLedgerEntry.objects.count(),
        list(
            CustomerAttribution.objects.order_by("id").values_list(
                "id",
                "user_id",
                "partner_channel_id",
            )
        ),
        list(Membership.objects.order_by("id").values_list("id", "role", "status")),
        list(
            PartnerChannel.objects.order_by("id").values_list(
                "id",
                "kind",
                "owner_user_id",
                "organization_id",
            )
        ),
    )


@ENABLED
@pytest.mark.django_db
def test_anonymous_is_authentication_required(client: Client) -> None:
    response = _get(client, None, uuid.uuid4())

    assert response.status_code == 401
    assert response.json() == {"code": "authentication_required"}
    assert response["Cache-Control"] == "no-store"
    assert "X-Partner-Role" not in response


@ENABLED
@pytest.mark.django_db
def test_flag_off_hides_the_list_before_query_validation(
    client: Client, settings
) -> None:
    settings.PARTNER_CHANNEL_ENABLED = False
    owner = _user("owner")
    channel = _individual(owner)

    response = _get(client, owner, channel.pk, sort="nope")

    assert response.status_code == 404
    assert response.json() == {"code": "partner_channel_disabled"}
    assert "X-Partner-Role" not in response


@ENABLED
@pytest.mark.django_db
def test_individual_owner_sees_only_current_channel_customers(client: Client) -> None:
    owner = _user("owner")
    stranger = _user("stranger")
    channel = _individual(owner)
    other = _team(stranger, "Other")
    ada = _user("ada", display_name="  Ada  ")
    bea = _user("bea")
    _attribute(ada, channel)
    _attribute(bea, other)
    _accrual(channel, owner.billing_account, ada, "1.250000")
    _accrual(other, other.organization.account, ada, "9.000000")
    before = _snapshot()

    response = _get(client, owner, channel.pk)
    denied = _get(client, stranger, channel.pk)

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    assert response["X-Partner-Role"] == "owner"
    assert type(response["X-Partner-Role"]) is str
    body = response.json()
    assert body["count"] == 1
    assert body["page"] == 1
    assert body["page_size"] == 50
    assert body["results"] == [
        {
            "customer_id": ada.pk,
            "email": ada.email,
            "display_name": "Ada",
            "attributed_at": body["results"][0]["attributed_at"],
            "credit_balance": "0.000000",
            "total_partner_earned": "1.250000",
            "accrual_count": 1,
        }
    ]
    assert body["results"][0]["attributed_at"].startswith("2026-10-01T12:00:00")
    assert bea.pk not in _ids(response)
    assert "9.000000" not in response.content.decode()
    assert str(owner.billing_account.pk) not in response.content.decode()
    assert denied.status_code == 403
    assert denied.json() == {"code": "partner_access_denied"}
    assert ada.email not in denied.content.decode()
    assert _snapshot() == before


@ENABLED
@pytest.mark.django_db
def test_history_and_pending_are_not_current_customers(client: Client) -> None:
    owner = _user("owner")
    channel = _individual(owner)
    elsewhere = _team(_user("other"), "Elsewhere")
    moved = _user("moved")
    pending_user = _user("pending")
    _attribute(moved, elsewhere)
    CustomerAttributionHistory.objects.create(
        user=moved,
        from_partner_channel=channel,
        to_partner_channel=elsewhere,
        changed_by=owner,
        changed_by_user_id_snapshot=owner.pk,
        change_reason="transfer",
        changed_at=timezone.now(),
        previous_attributed_at=_AT,
    )
    PendingPartnerAttribution.objects.create(
        user=pending_user,
        partner_channel=channel,
        invite_token_snapshot="token",
        expires_at=timezone.now() + timedelta(hours=1),
    )

    response = _get(client, owner, channel.pk)

    assert response.status_code == 200
    assert response.json()["count"] == 0
    assert response.json()["results"] == []


@ENABLED
@pytest.mark.django_db
def test_team_owner_admin_and_viewer_can_read(client: Client) -> None:
    owner = _user("owner")
    admin = _user("admin")
    viewer = _user("viewer")
    member = _user("member")
    suspended = _user("suspended")
    revoked = _user("revoked")
    channel = _team(owner, "Fleet")
    customer = _user("ada")
    _attribute(customer, channel)
    _membership(user=admin, channel=channel, role=MembershipRole.ADMIN)
    _membership(user=viewer, channel=channel, role=MembershipRole.VIEWER)
    _membership(user=member, channel=channel, role=MembershipRole.MEMBER)
    _membership(
        user=suspended,
        channel=channel,
        role=MembershipRole.ADMIN,
        status=MembershipStatus.SUSPENDED,
    )
    _membership(
        user=revoked,
        channel=channel,
        role=MembershipRole.VIEWER,
        status=MembershipStatus.REVOKED,
    )

    owner_response = _get(client, owner, channel.pk)
    admin_response = _get(client, admin, channel.pk)
    viewer_response = _get(client, viewer, channel.pk)

    assert owner_response.status_code == 200
    assert owner_response["X-Partner-Role"] == "owner"
    assert admin_response.status_code == 200
    assert admin_response["X-Partner-Role"] == "admin"
    assert viewer_response.status_code == 200
    assert viewer_response["X-Partner-Role"] == "viewer"
    member_response = _get(client, member, channel.pk)

    assert _ids(owner_response) == [customer.pk]
    assert _ids(admin_response) == [customer.pk]
    assert _ids(viewer_response) == [customer.pk]
    assert member_response.status_code == 200
    assert member_response["X-Partner-Role"] == "member"
    assert _ids(member_response) == [customer.pk]
    for actor in (suspended, revoked):
        denied = _get(client, actor, channel.pk)
        assert denied.status_code == 403
        assert denied.json() == {"code": "partner_access_denied"}
        assert "X-Partner-Role" not in denied


@ENABLED
@pytest.mark.django_db
def test_individual_and_team_lists_stay_separate(client: Client) -> None:
    user = _user("user")
    individual = _individual(user)
    team = _team(user, "Fleet")
    personal_customer = _user("personal")
    team_customer = _user("team")
    _attribute(personal_customer, individual)
    _attribute(team_customer, team)
    before = _snapshot()

    personal = _get(client, user, individual.pk)
    fleet = _get(client, user, team.pk)

    assert personal["X-Partner-Role"] == "owner"
    assert fleet["X-Partner-Role"] == "owner"
    assert _ids(personal) == [personal_customer.pk]
    assert _ids(fleet) == [team_customer.pk]
    assert _snapshot() == before


@ENABLED
@pytest.mark.django_db
def test_multiple_team_channels_stay_separate_and_legacy_stays_ambiguous(
    client: Client,
) -> None:
    user = _user("user")
    first = _team(_user("alpha-owner"), "Alpha")
    second = _team(_user("beta-owner"), "Beta")
    _membership(user=user, channel=first, role=MembershipRole.ADMIN)
    _membership(user=user, channel=second, role=MembershipRole.VIEWER)
    alpha_customer = _user("alpha")
    beta_customer = _user("beta")
    _attribute(alpha_customer, first)
    _attribute(beta_customer, second)

    alpha = _get(client, user, first.pk)
    beta = _get(client, user, second.pk)
    legacy = _legacy(client, user)

    assert alpha["X-Partner-Role"] == "admin"
    assert beta["X-Partner-Role"] == "viewer"
    assert _ids(alpha) == [alpha_customer.pk]
    assert _ids(beta) == [beta_customer.pk]
    assert legacy.status_code == 409
    assert legacy.json() == {"code": "partner_context_ambiguous"}


@ENABLED
@pytest.mark.django_db
def test_inaccessible_channel_does_not_fall_back(client: Client) -> None:
    user = _user("user")
    individual = _individual(user)
    team = _team(user, "Fleet")
    personal_customer = _user("personal")
    team_customer = _user("team")
    _attribute(personal_customer, individual)
    _attribute(team_customer, team)
    foreign = _individual(_user("foreign"))
    foreign_customer = _user("foreign-customer")
    _attribute(foreign_customer, foreign)
    unknown = uuid.uuid4()

    denied_foreign = _get(client, user, foreign.pk, sort="email")
    denied_unknown = _get(client, user, unknown, sort="email")
    allowed = _get(client, user, individual.pk, sort="email")

    assert denied_foreign.status_code == 403
    assert denied_unknown.status_code == 403
    assert denied_foreign.content == denied_unknown.content
    assert denied_foreign.json() == {"code": "partner_access_denied"}
    assert personal_customer.email not in denied_foreign.content.decode()
    assert team_customer.email not in denied_foreign.content.decode()
    assert foreign_customer.email not in denied_foreign.content.decode()
    assert allowed.status_code == 400
    assert allowed.json() == {"code": "invalid_sort"}
    assert _legacy(client, user).status_code == 200


@ENABLED
@pytest.mark.django_db
def test_inactive_channel_still_lists_current_customers(client: Client) -> None:
    owner = _user("owner")
    channel = _individual(owner, active=False)
    customer = _user("ada")
    _attribute(customer, channel)

    response = _get(client, owner, channel.pk)

    assert response.status_code == 200
    assert _ids(response) == [customer.pk]


@ENABLED
@pytest.mark.django_db
def test_malformed_channel_path_is_a_normal_404(client: Client) -> None:
    owner = _user("owner")

    response = client.get(
        "/api/v1/partner/channels/not-a-uuid/customers/",
        **_auth(owner),
    )

    assert response.status_code == 404
    assert b"partner_access_denied" not in response.content


@ENABLED
@pytest.mark.django_db
def test_individual_only_user_cannot_use_the_legacy_endpoint(client: Client) -> None:
    owner = _user("owner")
    channel = _individual(owner)
    _attribute(_user("ada"), channel)

    legacy = _legacy(client, owner)
    current = _get(client, owner, channel.pk)

    assert legacy.status_code == 403
    assert legacy.json() == {"code": "partner_access_denied"}
    assert current.status_code == 200
    assert current.json()["count"] == 1


def _keys(response, customer_id: int) -> set[str]:
    for row in response.json()["results"]:
        if row["customer_id"] == customer_id:
            return set(row)
    raise AssertionError("customer missing")


@ENABLED
@pytest.mark.django_db
def test_customer_fields_follow_the_role(client: Client) -> None:
    owner = _user("owner")
    admin = _user("admin")
    member = _user("member")
    viewer = _user("viewer")
    channel = _team(owner, "Fleet")
    customer = _user("ada")
    _attribute(customer, channel)
    _membership(user=admin, channel=channel, role=MembershipRole.ADMIN)
    _membership(user=member, channel=channel, role=MembershipRole.MEMBER)
    _membership(user=viewer, channel=channel, role=MembershipRole.VIEWER)
    credit_service.credit(
        customer.billing_account,
        Decimal("15.500000"),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id=f"bal-{uuid.uuid4()}",
        idempotency_key=f"bal-{uuid.uuid4()}",
    )
    identity = {"customer_id", "email", "display_name", "attributed_at"}
    owner_fields = identity | {
        "credit_balance",
        "total_partner_earned",
        "accrual_count",
    }
    member_fields = identity | {"credit_balance", "accrual_count"}

    owner_row = _get(client, owner, channel.pk).json()["results"][0]
    admin_row = _get(client, admin, channel.pk).json()["results"][0]
    member_response = _get(client, member, channel.pk)
    viewer_response = _get(client, viewer, channel.pk)

    assert set(owner_row) == owner_fields
    assert set(admin_row) == owner_fields
    assert owner_row["credit_balance"] == "15.500000"
    assert admin_row["credit_balance"] == "15.500000"
    assert owner_row["total_partner_earned"] == "0.000000"
    assert _keys(member_response, customer.pk) == member_fields
    assert member_response.json()["results"][0]["credit_balance"] == "15.500000"
    assert "total_partner_earned" not in member_response.content.decode()
    assert _keys(viewer_response, customer.pk) == identity
    assert b"credit_balance" not in viewer_response.content
    assert b"total_partner_earned" not in viewer_response.content
    assert b"accrual_count" not in viewer_response.content


@ENABLED
@pytest.mark.django_db
def test_missing_personal_account_is_null_and_is_not_created(client: Client) -> None:
    owner = _user("owner")
    channel = _individual(owner)
    customer = _user("ada")
    _attribute(customer, channel)
    customer.billing_account.delete()
    before = Account.objects.count()

    response = _get(client, owner, channel.pk)

    assert response.status_code == 200
    assert response.json()["results"][0]["credit_balance"] is None
    assert Account.objects.count() == before
    assert not Account.objects.filter(user=customer).exists()


@ENABLED
@pytest.mark.django_db
def test_member_cannot_open_other_partner_routes(client: Client) -> None:
    owner = _user("owner")
    member = _user("member")
    channel = _team(owner, "Fleet")
    _membership(user=member, channel=channel, role=MembershipRole.MEMBER)
    headers = _auth(member)
    paths = (
        f"/api/v1/partner/channels/{channel.pk}/summary/",
        f"/api/v1/partner/channels/{channel.pk}/grants/",
        f"/api/v1/partner/channels/{channel.pk}/invite-link/",
    )

    for path in paths:
        response = client.get(path, **headers)
        assert response.status_code == 403
        assert response.json() == {"code": "partner_access_denied"}
        assert "X-Partner-Role" not in response

    grant = client.post(
        f"/api/v1/partner/channels/{channel.pk}/grants/",
        data={"amount": "1.000000"},
        content_type="application/json",
        **headers,
    )
    assert grant.status_code == 403
    assert grant.json() == {"code": "partner_access_denied"}


@ENABLED
@pytest.mark.django_db
def test_customers_sort_defaults_and_hidden_fields_are_rejected(client: Client) -> None:
    owner = _user("owner")
    member = _user("member")
    viewer = _user("viewer")
    channel = _team(owner, "Fleet")
    _membership(user=member, channel=channel, role=MembershipRole.MEMBER)
    _membership(user=viewer, channel=channel, role=MembershipRole.VIEWER)
    earlier = _user("earlier")
    later = _user("later")
    _attribute(earlier, channel)
    later_row = _attribute(later, channel)
    later_row.attributed_at = _AT + timedelta(days=1)
    later_row.save(update_fields=["attributed_at"])
    _accrual(channel, owner.billing_account, earlier, "2.000000")

    owner_default = _ids(_get(client, owner, channel.pk))
    member_default = _ids(_get(client, member, channel.pk))
    viewer_default = _ids(_get(client, viewer, channel.pk))
    owner_explicit = _ids(
        _get(client, owner, channel.pk, sort="total_partner_earned", order="desc")
    )
    member_accruals = _ids(
        _get(client, member, channel.pk, sort="accrual_count", order="desc")
    )

    assert owner_default == owner_explicit == [earlier.pk, later.pk]
    assert member_default == viewer_default == [later.pk, earlier.pk]
    assert member_accruals == [earlier.pk, later.pk]
    for actor, sort in (
        (member, "total_partner_earned"),
        (viewer, "total_partner_earned"),
        (viewer, "accrual_count"),
        (owner, "credit_balance"),
    ):
        denied = _get(client, actor, channel.pk, sort=sort)
        assert denied.status_code == 400
        assert denied.json() == {"code": "invalid_sort"}
    for actor in (owner, member, viewer):
        allowed = _get(client, actor, channel.pk, sort="attributed_at", order="asc")
        assert allowed.status_code == 200
        assert _ids(allowed) == [earlier.pk, later.pk]


@ENABLED
@pytest.mark.django_db
def test_role_change_applies_on_the_next_customers_read(client: Client) -> None:
    owner = _user("owner")
    actor = _user("actor")
    channel = _team(owner, "Fleet")
    customer = _user("ada")
    _attribute(customer, channel)
    membership = Membership.objects.create(
        organization=channel.organization,
        user=actor,
        role=MembershipRole.VIEWER,
        status=MembershipStatus.ACTIVE,
    )

    as_viewer = _get(client, actor, channel.pk)
    membership.role = MembershipRole.MEMBER
    membership.save(update_fields=["role"])
    as_member = _get(client, actor, channel.pk)
    membership.status = MembershipStatus.SUSPENDED
    membership.save(update_fields=["status"])
    suspended = _get(client, actor, channel.pk)

    assert "credit_balance" not in as_viewer.json()["results"][0]
    assert "credit_balance" in as_member.json()["results"][0]
    assert "total_partner_earned" not in as_member.json()["results"][0]
    assert suspended.status_code == 403
    assert suspended.json() == {"code": "partner_access_denied"}
