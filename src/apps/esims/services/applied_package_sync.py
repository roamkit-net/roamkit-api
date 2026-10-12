"""Store the latest Airalo package rows for one eSIM."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.esims.models import Esim, EsimAppliedPackage, Topup
from apps.esims.services.package_history_service import _kind, _parse_datetime
from shared.providers.esim import SimPackageDTO, SimPackageHistoryProvider


def sync_applied_packages(esim: Esim, provider: SimPackageHistoryProvider) -> int:
    """Replace the stored package snapshot from one provider read.

    The provider call happens before the write. A failure before the write
    leaves the previous snapshot in place. Sequence 0 is the original SIM
    package. Later sequences follow top-up rows in sequence order.
    """
    rows = provider.list_sim_packages(esim.iccid)
    topups = list(esim.topups.order_by("sequence", "created_at", "pk"))
    assignments = _assign(topups, rows)
    now = timezone.now()
    seen_topup_ids = {topup.pk for topup, _row in assignments}

    with transaction.atomic():
        EsimAppliedPackage.objects.filter(esim=esim).delete()
        EsimAppliedPackage.objects.bulk_create(
            [_snapshot(esim, topup, row, synced_at=now) for topup, row in assignments]
        )
        for topup, row in assignments:
            if topup.provider_package_status == row.status:
                continue
            topup.provider_package_status = row.status
            topup.save(update_fields=["provider_package_status", "updated_at"])
        stale = esim.topups.exclude(pk__in=seen_topup_ids).exclude(
            provider_package_status=""
        )
        for topup in stale:
            topup.provider_package_status = ""
            topup.save(update_fields=["provider_package_status", "updated_at"])
    return len(assignments)


def _assign(
    topups: list[Topup], rows: list[SimPackageDTO]
) -> list[tuple[Topup, SimPackageDTO]]:
    esim_rows = [row for row in rows if _kind(row.plan_type) == "esim"]
    topup_rows = [row for row in rows if _kind(row.plan_type) != "esim"]
    assigned: list[tuple[Topup, SimPackageDTO]] = []
    used: set[str] = set()

    initial = next((topup for topup in topups if topup.sequence == 0), None)
    if initial is not None and esim_rows:
        match = next(
            (
                row
                for row in esim_rows
                if row.package_external_id == initial.package_external_id
            ),
            None,
        )
        if match is None and len(esim_rows) == 1:
            match = esim_rows[0]
        if match is not None:
            assigned.append((initial, match))
            used.add(match.instance_id)

    remaining = [row for row in topup_rows if row.instance_id not in used]
    by_package: dict[str, list[SimPackageDTO]] = {}
    for row in remaining:
        by_package.setdefault(row.package_external_id, []).append(row)

    for topup in topups:
        if topup.sequence is None or topup.sequence == 0:
            continue
        bucket = by_package.get(topup.package_external_id) or []
        if not bucket:
            continue
        assigned.append((topup, bucket.pop(0)))
    return assigned


def _snapshot(
    esim: Esim,
    topup: Topup,
    row: SimPackageDTO,
    *,
    synced_at,
) -> EsimAppliedPackage:
    price: Decimal | None = row.provider_price_usd
    return EsimAppliedPackage(
        esim=esim,
        topup=topup,
        instance_id=row.instance_id,
        sequence=topup.sequence if topup.sequence is not None else 0,
        package_external_id=row.package_external_id,
        kind=_kind(row.plan_type),
        status=row.status,
        data_allowance=row.data_allowance,
        validity_days=row.validity_days,
        is_unlimited=row.is_unlimited,
        amount_mb=row.amount_mb,
        remaining_mb=row.remaining_mb,
        activated_at=_parse_datetime(row.activated_at),
        expired_at=_parse_datetime(row.expired_at),
        finished_at=_parse_datetime(row.finished_at),
        provider_price_usd=price,
        synced_at=synced_at,
    )
