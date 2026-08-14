"""Tests for PackageHistoryService paid_usd matching and status pass-through."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.catalog.models import Package
from apps.esims.models import Esim, Topup
from apps.esims.services.package_history_service import PackageHistoryService
from apps.orders.models import Order
from apps.orders.product_snapshot import product_snapshot_kwargs
from shared.providers.esim import SimPackageDTO


def _dto(
    *,
    instance_id: str,
    status: str = "active",
    package_external_id: str = "topup-1gb",
    plan_type: str = "topup",
    remaining_mb: int | None = 100,
    is_unlimited: bool = False,
    provider_order_id: str | None = None,
    activated_at: str | None = "2026-08-12T10:50:00+00:00",
    expired_at: str | None = "2026-08-19T10:50:00+00:00",
    data_allowance: str = "1 GB",
    validity_days: int = 7,
) -> SimPackageDTO:
    return SimPackageDTO(
        instance_id=instance_id,
        status=status,
        remaining_mb=remaining_mb,
        activated_at=activated_at,
        expired_at=expired_at,
        finished_at=None,
        package_external_id=package_external_id,
        plan_type=plan_type,
        data_allowance=data_allowance,
        validity_days=validity_days,
        is_unlimited=is_unlimited,
        provider_order_id=provider_order_id,
    )


class FakeHistoryProvider:
    def __init__(self, rows: list[SimPackageDTO]) -> None:
        self.rows = rows
        self.calls: list[str] = []

    def list_sim_packages(self, iccid: str) -> list[SimPackageDTO]:
        self.calls.append(iccid)
        return self.rows


@pytest.fixture
def esim(db) -> Esim:
    user = User.objects.create_user(email="owner@example.com", password="secret123")
    package = Package.objects.create(
        external_id="pkg-us-1gb-7d",
        title="1 GB - 7 Days",
        operator_title="Op",
        country_code="US",
        data_allowance="1 GB",
        validity_days=7,
        price_usd=Decimal("11.50"),
        synced_at=timezone.now(),
    )
    order = Order.objects.create(
        account=user.billing_account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id="airalo-order-1",
        **product_snapshot_kwargs(package),
    )
    return Esim.objects.create(
        user=user,
        account=user.billing_account,
        order=order,
        iccid="891000000000001111",
        status=Esim.Status.PURCHASED,
    )


def _create_topup(
    esim: Esim,
    *,
    package_external_id: str,
    amount: Decimal,
    external_order_id: str = "",
    status: str = Topup.Status.FULFILLED,
    idempotency_key: str | None = None,
) -> Topup:
    return Topup.objects.create(
        account=esim.account,
        esim=esim,
        package_external_id=package_external_id,
        amount=amount,
        status=status,
        external_order_id=external_order_id,
        idempotency_key=idempotency_key
        or f"idem-{package_external_id}-{amount}-{external_order_id}",
    )


def _create_other_esim(owner: User, *, iccid: str) -> Esim:
    package = Package.objects.get(external_id="pkg-us-1gb-7d")
    order = Order.objects.create(
        account=owner.billing_account,
        package=package,
        status=Order.Status.FULFILLED,
        external_order_id=f"airalo-order-{iccid[-4:]}",
        **product_snapshot_kwargs(package),
    )
    return Esim.objects.create(
        user=owner,
        account=owner.billing_account,
        order=order,
        iccid=iccid,
        status=Esim.Status.PURCHASED,
    )


@pytest.mark.django_db
def test_initial_esim_row_uses_order_retail_when_unambiguous(esim: Esim) -> None:
    provider = FakeHistoryProvider(
        [_dto(instance_id="1", plan_type="sim", package_external_id="pkg-us-1gb-7d")]
    )
    rows = PackageHistoryService(provider).list_packages(esim)

    assert len(rows) == 1
    assert rows[0].kind == "esim"
    assert rows[0].paid_usd == Decimal("11.50")
    assert rows[0].created_at == esim.order.created_at
    assert rows[0].currency == "USD"


@pytest.mark.django_db
def test_initial_esim_identity_match_on_order_id(esim: Esim) -> None:
    provider = FakeHistoryProvider(
        [
            _dto(
                instance_id="1",
                plan_type="sim",
                package_external_id="pkg-us-1gb-7d",
                provider_order_id="airalo-order-1",
            )
        ]
    )
    rows = PackageHistoryService(provider).list_packages(esim)
    assert rows[0].paid_usd == Decimal("11.50")


@pytest.mark.django_db
def test_initial_esim_wrong_order_id_stays_null(esim: Esim) -> None:
    provider = FakeHistoryProvider(
        [
            _dto(
                instance_id="1",
                plan_type="sim",
                package_external_id="pkg-us-1gb-7d",
                provider_order_id="someone-else",
            )
        ]
    )
    rows = PackageHistoryService(provider).list_packages(esim)
    assert rows[0].paid_usd is None


@pytest.mark.django_db
def test_topup_identity_match_uses_local_amount(esim: Esim) -> None:
    _create_topup(
        esim,
        package_external_id="topup-1gb",
        amount=Decimal("3.20"),
        external_order_id="ord-a",
    )
    provider = FakeHistoryProvider([_dto(instance_id="10", provider_order_id="ord-a")])
    rows = PackageHistoryService(provider).list_packages(esim)
    assert rows[0].paid_usd == Decimal("3.20")
    assert rows[0].kind == "topup"


@pytest.mark.django_db
def test_repeated_same_package_id_does_not_guess_paid_usd(esim: Esim) -> None:
    _create_topup(
        esim,
        package_external_id="topup-1gb",
        amount=Decimal("3.20"),
        external_order_id="ord-a",
    )
    _create_topup(
        esim,
        package_external_id="topup-1gb",
        amount=Decimal("4.00"),
        external_order_id="ord-b",
    )
    provider = FakeHistoryProvider(
        [
            _dto(instance_id="10", package_external_id="topup-1gb"),
            _dto(instance_id="11", package_external_id="topup-1gb"),
        ]
    )
    rows = PackageHistoryService(provider).list_packages(esim)
    assert [row.paid_usd for row in rows] == [None, None]


@pytest.mark.django_db
def test_unambiguous_package_id_match_when_no_order_identity(esim: Esim) -> None:
    _create_topup(
        esim,
        package_external_id="topup-1gb",
        amount=Decimal("3.20"),
    )
    provider = FakeHistoryProvider([_dto(instance_id="10")])
    rows = PackageHistoryService(provider).list_packages(esim)
    assert rows[0].paid_usd == Decimal("3.20")


@pytest.mark.django_db
def test_provider_order_id_without_local_match_does_not_fallback(esim: Esim) -> None:
    _create_topup(
        esim,
        package_external_id="topup-1gb",
        amount=Decimal("3.20"),
        external_order_id="ord-other",
    )
    provider = FakeHistoryProvider(
        [_dto(instance_id="10", provider_order_id="ord-missing")]
    )
    rows = PackageHistoryService(provider).list_packages(esim)
    assert rows[0].paid_usd is None


@pytest.mark.django_db
def test_failed_topup_is_not_used_for_paid_usd(esim: Esim) -> None:
    _create_topup(
        esim,
        package_external_id="topup-1gb",
        amount=Decimal("3.20"),
        status=Topup.Status.FAILED,
    )
    provider = FakeHistoryProvider([_dto(instance_id="10")])
    rows = PackageHistoryService(provider).list_packages(esim)
    assert rows[0].paid_usd is None


@pytest.mark.django_db
def test_unknown_status_is_preserved(esim: Esim) -> None:
    provider = FakeHistoryProvider([_dto(instance_id="10", status="unknown")])
    rows = PackageHistoryService(provider).list_packages(esim)
    assert rows[0].status == "unknown"


@pytest.mark.django_db
def test_unlimited_remaining_mb_stays_null(esim: Esim) -> None:
    provider = FakeHistoryProvider(
        [
            _dto(
                instance_id="10",
                is_unlimited=True,
                remaining_mb=0,
                data_allowance="Unlimited",
            )
        ]
    )
    rows = PackageHistoryService(provider).list_packages(esim)
    assert rows[0].is_unlimited is True
    assert rows[0].remaining_mb is None


@pytest.mark.django_db
def test_not_active_status_is_preserved(esim: Esim) -> None:
    provider = FakeHistoryProvider([_dto(instance_id="10", status="not_active")])
    rows = PackageHistoryService(provider).list_packages(esim)
    assert rows[0].status == "not_active"


@pytest.mark.django_db
def test_two_same_package_topups_match_on_instance_id(esim: Esim) -> None:
    _create_topup(
        esim,
        package_external_id="discover-in-7days-1gb-px-topup",
        amount=Decimal("3.20"),
        external_order_id="2314160",
    )
    _create_topup(
        esim,
        package_external_id="discover-in-7days-1gb-px-topup",
        amount=Decimal("3.20"),
        external_order_id="2322403",
    )
    provider = FakeHistoryProvider(
        [
            _dto(
                instance_id="2314160",
                package_external_id="discover-in-7days-1gb-px-topup",
            ),
            _dto(
                instance_id="2322403",
                package_external_id="discover-in-7days-1gb-px-topup",
            ),
        ]
    )
    rows = PackageHistoryService(provider).list_packages(esim)
    assert [row.id for row in rows] == ["2314160", "2322403"]
    assert [row.paid_usd for row in rows] == [Decimal("3.20"), Decimal("3.20")]


@pytest.mark.django_db
def test_instance_id_match_trims_whitespace(esim: Esim) -> None:
    _create_topup(
        esim,
        package_external_id="topup-1gb",
        amount=Decimal("3.20"),
        external_order_id="  2314160  ",
    )
    provider = FakeHistoryProvider([_dto(instance_id="  2314160  ")])
    rows = PackageHistoryService(provider).list_packages(esim)
    assert rows[0].paid_usd == Decimal("3.20")


@pytest.mark.django_db
def test_unknown_instance_id_stays_null_when_package_fallback_ambiguous(
    esim: Esim,
) -> None:
    _create_topup(
        esim,
        package_external_id="topup-1gb",
        amount=Decimal("3.20"),
        external_order_id="2314160",
    )
    _create_topup(
        esim,
        package_external_id="topup-1gb",
        amount=Decimal("3.20"),
        external_order_id="2322403",
    )
    provider = FakeHistoryProvider([_dto(instance_id="999999")])
    rows = PackageHistoryService(provider).list_packages(esim)
    assert rows[0].paid_usd is None


@pytest.mark.django_db
def test_blank_instance_id_does_not_pick_a_topup(esim: Esim) -> None:
    _create_topup(
        esim,
        package_external_id="topup-1gb",
        amount=Decimal("3.20"),
        external_order_id="2314160",
    )
    _create_topup(
        esim,
        package_external_id="topup-1gb",
        amount=Decimal("4.00"),
        external_order_id="2322403",
    )
    provider = FakeHistoryProvider([_dto(instance_id="   ")])
    rows = PackageHistoryService(provider).list_packages(esim)
    assert rows[0].paid_usd is None


@pytest.mark.django_db
def test_local_topup_is_assigned_to_at_most_one_history_row(esim: Esim) -> None:
    _create_topup(
        esim,
        package_external_id="topup-1gb",
        amount=Decimal("3.20"),
        external_order_id="2314160",
    )
    provider = FakeHistoryProvider(
        [
            _dto(instance_id="2314160", package_external_id="topup-1gb"),
            _dto(instance_id="999999", package_external_id="topup-1gb"),
        ]
    )
    rows = PackageHistoryService(provider).list_packages(esim)
    assert [row.paid_usd for row in rows] == [Decimal("3.20"), None]


@pytest.mark.django_db
def test_duplicate_external_order_id_stays_unmatched(esim: Esim) -> None:
    _create_topup(
        esim,
        package_external_id="topup-1gb",
        amount=Decimal("3.20"),
        external_order_id="2314160",
        idempotency_key="idem-dup-a",
    )
    _create_topup(
        esim,
        package_external_id="topup-1gb",
        amount=Decimal("3.20"),
        external_order_id="2314160",
        idempotency_key="idem-dup-b",
    )
    provider = FakeHistoryProvider([_dto(instance_id="2314160")])
    rows = PackageHistoryService(provider).list_packages(esim)
    assert rows[0].paid_usd is None


@pytest.mark.django_db
def test_other_esim_topup_is_never_a_candidate(esim: Esim) -> None:
    other = _create_other_esim(esim.user, iccid="891000000000002222")
    _create_topup(
        other,
        package_external_id="topup-1gb",
        amount=Decimal("3.20"),
        external_order_id="2314160",
    )
    provider = FakeHistoryProvider([_dto(instance_id="2314160")])
    rows = PackageHistoryService(provider).list_packages(esim)
    assert rows[0].paid_usd is None
