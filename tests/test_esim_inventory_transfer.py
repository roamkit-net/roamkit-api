"""Explicit eSIM inventory transfer. Historical billing stays put."""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.admin.sites import AdminSite
from django.test import Client, RequestFactory, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import AccountKind, CreditLedgerEntry, LedgerReferenceType
from apps.billing.services import credit_service
from apps.catalog.models import Package
from apps.esims.admin import EsimAdmin, EsimInventoryTransferForm
from apps.esims.exceptions import (
    EsimOwnershipChanged,
    EsimTransferDestinationError,
    EsimTransferInProgressError,
)
from apps.esims.models import Esim, EsimAutoTopupPolicy, Topup
from apps.esims.services.auto_topup_service import AutoTopupService
from apps.esims.services.inventory_transfer import transfer_esim_inventory
from apps.esims.services.topup_service import TopupService
from apps.orders.models import Order
from apps.organizations.services.account_binding import create_organization
from shared.providers.esim import TopupPackage, TopupResult, UsageDTO


def _user(prefix: str) -> User:
    return User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4()}@example.com",
        password="SecurePass1!",
    )


def _staff() -> User:
    staff = _user("staff")
    staff.is_staff = True
    staff.is_superuser = True
    staff.save(update_fields=["is_staff", "is_superuser"])
    return staff


def _package() -> Package:
    return Package.objects.create(
        external_id=f"pkg-{uuid.uuid4()}",
        title="1 GB - 7 Days",
        operator_title="Change",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("11.50"),
        synced_at=timezone.now(),
    )


def _esim(owner: User, package: Package) -> Esim:
    order = Order.objects.create(
        account=owner.billing_account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id=f"ext-{uuid.uuid4()}",
        customer_ref="ref",
    )
    return Esim.objects.create(
        user=owner,
        account=owner.billing_account,
        order=order,
        iccid=f"891{uuid.uuid4().int % 10**15:015d}"[:18],
        status=Esim.Status.INSTALLED,
    )


def _topup_package() -> TopupPackage:
    return TopupPackage(
        external_id="topup-1gb",
        title="1 GB Top-up",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("5.00"),
        net_price_usd=Decimal("4.50"),
        is_unlimited=False,
        plan_type="topup",
    )


class FakeTopupProvider:
    def __init__(self) -> None:
        self.topups = [_topup_package()]
        self.submit_calls: list[tuple[str, str]] = []

    def list_topups(self, iccid: str) -> list[TopupPackage]:
        return self.topups

    def submit_topup(self, iccid: str, package_id: str) -> TopupResult:
        self.submit_calls.append((iccid, package_id))
        return TopupResult(
            external_order_id="top-ext",
            code="TOP",
            package_id=package_id,
            iccid=iccid,
            currency="USD",
            price_usd=Decimal("5.00"),
            customer_ref="ref",
        )

    def get_usage(self, iccid: str) -> UsageDTO:
        raise AssertionError("get_usage is not used here")


def _policy(owner: User, esim: Esim, **kwargs) -> EsimAutoTopupPolicy:
    defaults = {
        "account": owner.billing_account,
        "package_id": "topup-1gb",
        "enabled": True,
        "status": EsimAutoTopupPolicy.Status.ACTIVE,
        "reason": "",
        "expiry_enabled": False,
        "usage_mode": EsimAutoTopupPolicy.UsageMode.ZERO,
        "renew_mode": EsimAutoTopupPolicy.RenewMode.UNTIL_FUNDS,
        "version": 3,
    }
    defaults.update(kwargs)
    return EsimAutoTopupPolicy.objects.create(esim=esim, **defaults)


@pytest.mark.django_db
def test_transfer_moves_inventory_and_leaves_billing_on_the_source() -> None:
    owner = _user("owner")
    dest = _user("dest")
    package = _package()
    esim = _esim(owner, package)
    order_account_id = esim.order.account_id
    historical = Topup.objects.create(
        account=owner.billing_account,
        esim=esim,
        package_external_id="past-package",
        amount=Decimal("5.000000"),
        status=Topup.Status.FULFILLED,
        idempotency_key=f"past-{uuid.uuid4()}",
    )
    credit_service.credit(
        owner.billing_account,
        Decimal("8.000000"),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id="fund-owner",
        idempotency_key=f"fund-owner-{owner.pk}",
    )
    credit_service.credit(
        dest.billing_account,
        Decimal("3.000000"),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id="fund-dest",
        idempotency_key=f"fund-dest-{dest.pk}",
    )
    owner.billing_account.refresh_from_db()
    dest.billing_account.refresh_from_db()
    owner_balance = owner.billing_account.balance
    dest_balance = dest.billing_account.balance
    ledger_ids = set(CreditLedgerEntry.objects.values_list("pk", flat=True))
    old_account_id = owner.billing_account.pk

    with (
        patch.object(credit_service, "debit") as debit,
        patch.object(credit_service, "credit") as credit,
    ):
        result = transfer_esim_inventory(esim_id=esim.pk, destination_user=dest)

    assert result.transferred is True
    assert result.auto_topup_disabled is False
    assert result.old_account_id == old_account_id
    assert result.new_account_id == dest.billing_account.pk
    debit.assert_not_called()
    credit.assert_not_called()

    esim.refresh_from_db()
    historical.refresh_from_db()
    esim.order.refresh_from_db()
    assert esim.account_id == dest.billing_account.pk
    assert esim.user_id == dest.pk
    assert esim.assigned_user_id == dest.pk
    assert esim.order.account_id == order_account_id
    assert historical.account_id == old_account_id
    assert set(CreditLedgerEntry.objects.values_list("pk", flat=True)) == ledger_ids
    owner.billing_account.refresh_from_db()
    dest.billing_account.refresh_from_db()
    assert owner.billing_account.balance == owner_balance
    assert dest.billing_account.balance == dest_balance
    assert Esim.objects.filter(account=dest.billing_account, pk=esim.pk).exists()
    assert not Esim.objects.filter(account_id=old_account_id, pk=esim.pk).exists()


@pytest.mark.django_db
def test_same_personal_account_is_a_noop() -> None:
    owner = _user("owner")
    esim = _esim(owner, _package())
    policy = _policy(owner, esim)

    result = transfer_esim_inventory(esim_id=esim.pk, destination_user=owner)

    assert result.transferred is False
    assert result.auto_topup_disabled is False
    assert result.old_account_id == result.new_account_id
    esim.refresh_from_db()
    policy.refresh_from_db()
    assert esim.account_id == owner.billing_account.pk
    assert policy.enabled is True
    assert policy.status == EsimAutoTopupPolicy.Status.ACTIVE
    assert policy.version == 3
    assert policy.account_id == owner.billing_account.pk


@pytest.mark.django_db
def test_destination_without_personal_account_is_rejected() -> None:
    owner = _user("owner")
    dest = _user("dest")
    esim = _esim(owner, _package())
    dest.billing_account.delete()

    with pytest.raises(EsimTransferDestinationError):
        transfer_esim_inventory(esim_id=esim.pk, destination_user=dest)

    esim.refresh_from_db()
    assert esim.account_id == owner.billing_account.pk
    assert esim.user_id == owner.pk


@pytest.mark.django_db
def test_organization_account_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _user("owner")
    dest = _user("dest")
    esim = _esim(owner, _package())
    org = create_organization(name=f"Fleet {uuid.uuid4()}", actor=owner)

    monkeypatch.setattr(
        "apps.esims.services.inventory_transfer._load_destination_account",
        lambda user: org.account,
    )

    with pytest.raises(EsimTransferDestinationError):
        transfer_esim_inventory(esim_id=esim.pk, destination_user=dest)

    esim.refresh_from_db()
    assert esim.account_id == owner.billing_account.pk
    assert esim.account_id != org.account_id
    assert org.account.kind == AccountKind.ORGANIZATION


@pytest.mark.django_db
def test_fulfilling_topup_blocks_transfer() -> None:
    owner = _user("owner")
    dest = _user("dest")
    esim = _esim(owner, _package())
    topup = Topup.objects.create(
        account=owner.billing_account,
        esim=esim,
        package_external_id="in-flight",
        amount=Decimal("5.000000"),
        status=Topup.Status.FULFILLING,
        idempotency_key=f"flight-{uuid.uuid4()}",
    )
    policy = _policy(owner, esim)

    with pytest.raises(EsimTransferInProgressError):
        transfer_esim_inventory(esim_id=esim.pk, destination_user=dest)

    esim.refresh_from_db()
    topup.refresh_from_db()
    policy.refresh_from_db()
    assert esim.account_id == owner.billing_account.pk
    assert esim.user_id == owner.pk
    assert esim.assigned_user_id is None
    assert topup.account_id == owner.billing_account.pk
    assert policy.account_id == owner.billing_account.pk
    assert policy.enabled is True
    assert policy.status == EsimAutoTopupPolicy.Status.ACTIVE
    assert policy.version == 3


@pytest.mark.django_db
def test_active_policy_is_disabled_and_retargeted() -> None:
    owner = _user("owner")
    dest = _user("dest")
    esim = _esim(owner, _package())
    historical = Topup.objects.create(
        account=owner.billing_account,
        esim=esim,
        package_external_id="past-package",
        amount=Decimal("5.000000"),
        status=Topup.Status.FULFILLED,
        idempotency_key=f"past-{uuid.uuid4()}",
    )
    policy = _policy(owner, esim, last_topup=historical)

    result = transfer_esim_inventory(esim_id=esim.pk, destination_user=dest)

    assert result.transferred is True
    assert result.auto_topup_disabled is True
    policy.refresh_from_db()
    historical.refresh_from_db()
    assert policy.account_id == dest.billing_account.pk
    assert policy.enabled is False
    assert policy.status == EsimAutoTopupPolicy.Status.DISABLED
    assert policy.reason == EsimAutoTopupPolicy.Reason.MANUAL_PAUSE
    assert policy.version == 4
    assert policy.last_topup_id == historical.pk
    assert historical.account_id == owner.billing_account.pk


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_topup_rechecks_ownership_after_locking_the_esim() -> None:
    owner = _user("owner")
    dest = _user("dest")
    esim = _esim(owner, _package())
    credit_service.credit(
        owner.billing_account,
        Decimal("20.000000"),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id="fund",
        idempotency_key=f"fund-race-{owner.pk}",
    )
    provider = FakeTopupProvider()

    def list_topups(iccid: str) -> list[TopupPackage]:
        transfer_esim_inventory(esim_id=esim.pk, destination_user=dest)
        return provider.topups

    provider.list_topups = list_topups
    owner.billing_account.refresh_from_db()
    dest.billing_account.refresh_from_db()
    owner_balance = owner.billing_account.balance
    dest_balance = dest.billing_account.balance

    with pytest.raises(EsimOwnershipChanged):
        TopupService(provider).purchase(
            esim,
            package_id="topup-1gb",
            idempotency_key=f"race-{uuid.uuid4()}",
            account=owner.billing_account,
        )

    assert provider.submit_calls == []
    assert Topup.objects.filter(esim=esim).count() == 0
    owner.billing_account.refresh_from_db()
    dest.billing_account.refresh_from_db()
    assert owner.billing_account.balance == owner_balance
    assert dest.billing_account.balance == dest_balance
    esim.refresh_from_db()
    assert esim.account_id == dest.billing_account.pk


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True)
def test_replay_still_returns_the_original_topup_after_transfer() -> None:
    owner = _user("owner")
    dest = _user("dest")
    esim = _esim(owner, _package())
    credit_service.credit(
        owner.billing_account,
        Decimal("20.000000"),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id="fund",
        idempotency_key=f"fund-replay-{owner.pk}",
    )
    provider = FakeTopupProvider()
    service = TopupService(provider)
    key = f"replay-{uuid.uuid4()}"
    first = service.purchase(
        esim,
        package_id="topup-1gb",
        idempotency_key=key,
    )
    transfer_esim_inventory(esim_id=esim.pk, destination_user=dest)
    esim.refresh_from_db()

    second = service.purchase(
        esim,
        package_id="topup-1gb",
        idempotency_key=key,
        account=owner.billing_account,
    )

    assert second.pk == first.pk
    assert second.account_id == owner.billing_account.pk
    assert provider.submit_calls == [(esim.iccid, "topup-1gb")]
    assert Topup.objects.filter(esim=esim).count() == 1


@pytest.mark.django_db
@override_settings(
    BILLING_ENABLED=True,
    AUTO_TOPUP_ENABLED=True,
    AUTO_TOPUP_ROLLOUT_MODE="all",
)
def test_prepared_auto_topup_does_not_charge_the_new_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _user("owner")
    dest = _user("dest")
    esim = _esim(owner, _package())
    policy = _policy(owner, esim)
    credit_service.credit(
        dest.billing_account,
        Decimal("20.000000"),
        reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
        reference_id="fund-new",
        idempotency_key=f"fund-new-{dest.pk}",
    )
    old_account = owner.billing_account
    transfer_esim_inventory(esim_id=esim.pk, destination_user=dest)
    dest.billing_account.refresh_from_db()
    dest_balance = dest.billing_account.balance
    provider = FakeTopupProvider()
    service = AutoTopupService(provider)

    def prepared(_policy_id):
        return (
            esim,
            "topup-1gb",
            f"auto-race-{uuid.uuid4()}",
            "usage_zero",
            old_account,
        )

    monkeypatch.setattr(service, "_prepare", prepared)

    assert service._evaluate_one_inner(policy.pk) == "skipped"
    assert provider.submit_calls == []
    assert not Topup.objects.filter(account=dest.billing_account).exists()
    assert not Topup.objects.filter(
        account=old_account, status=Topup.Status.FULFILLING
    ).exists()
    dest.billing_account.refresh_from_db()
    assert dest.billing_account.balance == dest_balance


@pytest.mark.django_db
def test_admin_transfer_is_a_separate_audited_action() -> None:
    staff = _staff()
    owner = _user("owner")
    dest = _user("dest")
    naked = _user("naked")
    naked.billing_account.delete()
    esim = _esim(owner, _package())
    policy = _policy(owner, esim)
    client = Client()
    client.force_login(staff)
    transfer_url = f"/admin/esims/esim/{esim.pk}/transfer/"
    change_url = f"/admin/esims/esim/{esim.pk}/change/"

    form = EsimInventoryTransferForm()
    destination_ids = set(
        form.fields["destination_user"].queryset.values_list("pk", flat=True)
    )
    assert dest.pk in destination_ids
    assert naked.pk not in destination_ids
    assert (
        not form.fields["destination_user"]
        .queryset.filter(billing_account__kind=AccountKind.ORGANIZATION)
        .exists()
    )

    request = RequestFactory().get("/")
    request.user = staff
    readonly = EsimAdmin(Esim, AdminSite()).get_readonly_fields(request, esim)
    assert {"account", "user", "assigned_user", "order"} <= set(readonly)

    before = client.get(transfer_url)
    assert before.status_code == 200
    assert b"csrfmiddlewaretoken" in before.content
    assert b"are not moved" in before.content
    assert b"disabled" in before.content
    assert str(esim.iccid).encode() in before.content
    esim.refresh_from_db()
    assert esim.account_id == owner.billing_account.pk
    assert LogEntry.objects.filter(object_id=str(esim.pk)).count() == 0

    change_page = client.get(change_url)
    assert change_page.status_code == 200
    assert b"Transfer" in change_page.content
    assert b'name="account"' not in change_page.content
    assert b'name="user"' not in change_page.content
    assert b'name="assigned_user"' not in change_page.content
    assert b'name="order"' not in change_page.content

    with patch(
        "apps.esims.admin.transfer_esim_inventory",
        wraps=transfer_esim_inventory,
    ) as transfer:
        ignored = client.post(
            change_url,
            {
                "iccid": esim.iccid,
                "status": esim.status,
                "activation_policy": esim.activation_policy,
                "user": dest.pk,
                "account": dest.billing_account.pk,
                "assigned_user": dest.pk,
                "note": "unchanged owner",
            },
        )
        transfer.assert_not_called()
    assert ignored.status_code in (200, 302)
    esim.refresh_from_db()
    assert esim.account_id == owner.billing_account.pk
    assert esim.user_id == owner.pk

    noop_logs = LogEntry.objects.filter(object_id=str(esim.pk)).count()
    noop = client.post(transfer_url, {"destination_user": owner.pk})
    assert noop.status_code == 302
    assert LogEntry.objects.filter(object_id=str(esim.pk)).count() == noop_logs

    response = client.post(transfer_url, {"destination_user": dest.pk})
    assert response.status_code == 302
    esim.refresh_from_db()
    policy.refresh_from_db()
    assert esim.account_id == dest.billing_account.pk
    assert esim.user_id == dest.pk
    logs = [
        entry
        for entry in LogEntry.objects.filter(object_id=str(esim.pk), action_flag=CHANGE)
        if "Transferred inventory" in entry.get_change_message()
    ]
    assert len(logs) == 1
    log = logs[0]
    assert log.user_id == staff.pk
    message = log.get_change_message()
    assert str(owner.billing_account.pk) in message
    assert str(dest.billing_account.pk) in message
    assert dest.email in message
    assert "Auto top-up disabled" in message
    assert policy.status == EsimAutoTopupPolicy.Status.DISABLED


@pytest.mark.django_db
def test_admin_transfer_requires_change_permission() -> None:
    staff = _user("limited")
    staff.is_staff = True
    staff.is_superuser = False
    staff.save(update_fields=["is_staff", "is_superuser"])
    owner = _user("owner")
    esim = _esim(owner, _package())
    client = Client()
    client.force_login(staff)

    response = client.get(f"/admin/esims/esim/{esim.pk}/transfer/")

    assert response.status_code == 403
    esim.refresh_from_db()
    assert esim.user_id == owner.pk
