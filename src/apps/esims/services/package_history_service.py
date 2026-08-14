"""Applied package history for an owned eSIM (provider + local spend)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.esims.models import Topup
from shared.providers.esim import SimPackageDTO

if TYPE_CHECKING:
    from apps.esims.models import Esim
    from shared.providers.esim import SimPackageHistoryProvider


@dataclass(frozen=True)
class AppliedPackage:
    """Customer-facing applied package row (no wholesale fields)."""

    id: str
    kind: str
    status: str
    data_allowance: str
    validity_days: int
    is_unlimited: bool
    remaining_mb: int | None
    created_at: datetime | None
    activated_at: str | None
    expires_at: str | None
    paid_usd: Decimal | None
    currency: str


class PackageHistoryService:
    """Maps provider package history and attaches local ``paid_usd`` safely."""

    def __init__(self, provider: SimPackageHistoryProvider) -> None:
        self.provider = provider

    def list_packages(self, esim: Esim) -> list[AppliedPackage]:
        rows = self.provider.list_sim_packages(esim.iccid)
        currency = (esim.order.currency or "").strip() or "USD"
        paid_by_instance, created_by_instance = self._match_spend(esim, rows)
        return [
            AppliedPackage(
                id=row.instance_id,
                kind=_kind(row.plan_type),
                status=row.status,
                data_allowance=row.data_allowance,
                validity_days=row.validity_days,
                is_unlimited=row.is_unlimited,
                remaining_mb=None if row.is_unlimited else row.remaining_mb,
                created_at=created_by_instance.get(row.instance_id)
                or _parse_datetime(row.activated_at),
                activated_at=row.activated_at,
                expires_at=row.expired_at,
                paid_usd=paid_by_instance.get(row.instance_id),
                currency=currency,
            )
            for row in rows
        ]

    def _match_spend(
        self, esim: Esim, rows: list[SimPackageDTO]
    ) -> tuple[dict[str, Decimal], dict[str, datetime]]:
        paid: dict[str, Decimal] = {}
        created: dict[str, datetime] = {}

        esim_rows = [row for row in rows if _kind(row.plan_type) == "esim"]
        topup_rows = [row for row in rows if _kind(row.plan_type) != "esim"]

        self._match_initial_order(esim, esim_rows, paid, created)
        self._match_topups(esim, topup_rows, paid, created)
        return paid, created

    def _match_initial_order(
        self,
        esim: Esim,
        esim_rows: list[SimPackageDTO],
        paid: dict[str, Decimal],
        created: dict[str, datetime],
    ) -> None:
        order = esim.order
        retail = order.retail_price_usd
        order_id = (order.external_order_id or "").strip()

        identity_hits = [
            row
            for row in esim_rows
            if row.provider_order_id and order_id and row.provider_order_id == order_id
        ]
        if len(identity_hits) == 1:
            self._assign(
                identity_hits[0].instance_id,
                retail,
                order.created_at,
                paid,
                created,
            )
            return
        if identity_hits:
            return
        if len(esim_rows) == 1 and not esim_rows[0].provider_order_id:
            self._assign(
                esim_rows[0].instance_id,
                retail,
                order.created_at,
                paid,
                created,
            )

    def _match_topups(
        self,
        esim: Esim,
        topup_rows: list[SimPackageDTO],
        paid: dict[str, Decimal],
        created: dict[str, datetime],
    ) -> None:
        unused = list(
            esim.topups.filter(status=Topup.Status.FULFILLED).order_by("created_at")
        )
        used_ids: set[object] = set()

        for row in topup_rows:
            if not row.provider_order_id:
                continue
            match = next(
                (
                    topup
                    for topup in unused
                    if topup.pk not in used_ids
                    and (topup.external_order_id or "").strip() == row.provider_order_id
                ),
                None,
            )
            if match is None:
                continue
            used_ids.add(match.pk)
            self._assign(row.instance_id, match.amount, match.created_at, paid, created)

        unmatched_rows = [
            row
            for row in topup_rows
            if row.instance_id not in paid and not row.provider_order_id
        ]
        unused_topups = [topup for topup in unused if topup.pk not in used_ids]
        by_package: dict[str, list[SimPackageDTO]] = {}
        for row in unmatched_rows:
            key = row.package_external_id
            if not key:
                continue
            by_package.setdefault(key, []).append(row)

        locals_by_package: dict[str, list[Topup]] = {}
        for topup in unused_topups:
            locals_by_package.setdefault(topup.package_external_id, []).append(topup)

        for package_id, hist in by_package.items():
            locals_ = locals_by_package.get(package_id, [])
            if len(hist) == 1 and len(locals_) == 1:
                match = locals_[0]
                self._assign(
                    hist[0].instance_id, match.amount, match.created_at, paid, created
                )

    @staticmethod
    def _assign(
        instance_id: str,
        amount: Decimal | None,
        created_at: datetime | None,
        paid: dict[str, Decimal],
        created: dict[str, datetime],
    ) -> None:
        if amount is not None:
            paid[instance_id] = amount
        if created_at is not None:
            created[instance_id] = created_at


def _kind(plan_type: str) -> str:
    token = (plan_type or "").strip().lower()
    if token in {"sim", "esim"}:
        return "esim"
    return "topup"


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T", 1)
    parsed = parse_datetime(normalized)
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed)
    return parsed
