"""Read-only partner customer list (ADR 023).

The caller passes an already resolved ``PartnerChannel`` and validated query
parameters. Earnings follow ``customer_user_id_snapshot`` on this channel.
The current attribution row only decides who is listed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.db.models import (
    Count,
    DecimalField,
    IntegerField,
    OuterRef,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce

from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerChannel,
    PartnerMarginAccrual,
)

_ZERO = Decimal("0.000000")
_MONEY = DecimalField(max_digits=20, decimal_places=6)
_USER_ID_RE = re.compile(r"^(?:0|[1-9]\d*)$")
_BIGINT_MAX = 2**63 - 1
_SORT_FIELDS = {
    "attributed_at": "attributed_at",
    "total_partner_earned": "total_partner_earned",
    "accrual_count": "accrual_count",
}


@dataclass(frozen=True, slots=True)
class PartnerCustomersQuery:
    """Already validated list parameters. ``q`` is trimmed."""

    page: int
    page_size: int
    sort: str
    order: str
    q: str


@dataclass(frozen=True, slots=True)
class PartnerCustomerRow:
    customer_id: int
    email: str
    attributed_at: datetime
    total_partner_earned: Decimal
    accrual_count: int


@dataclass(frozen=True, slots=True)
class PartnerCustomersPage:
    count: int
    page: int
    page_size: int
    results: tuple[PartnerCustomerRow, ...]


class PartnerCustomersService:
    """List current attributions with snapshot earnings. No locks or writes."""

    def list_customers(
        self,
        partner_channel: PartnerChannel,
        query: PartnerCustomersQuery,
    ) -> PartnerCustomersPage:
        base = CustomerAttribution.objects.filter(partner_channel=partner_channel)
        customer_id = _customer_id_query(query.q)
        if customer_id is not None:
            base = base.filter(user_id=customer_id)
        elif query.q:
            base = base.filter(user__email__iexact=query.q)
        count = base.count()
        direction = "-" if query.order == "desc" else ""
        ordering = (
            f"{direction}{_SORT_FIELDS[query.sort]}",
            "user_id",
        )
        start = (query.page - 1) * query.page_size
        rows = (
            _with_snapshot_totals(base, partner_channel)
            .select_related("user")
            .order_by(*ordering)[start : start + query.page_size]
        )
        return PartnerCustomersPage(
            count=count,
            page=query.page,
            page_size=query.page_size,
            results=tuple(_row(item) for item in rows),
        )


def _with_snapshot_totals(base, partner_channel: PartnerChannel):
    accruals = PartnerMarginAccrual.objects.filter(
        partner_channel=partner_channel,
        customer_user_id_snapshot=OuterRef("user_id"),
    ).order_by()
    earned = (
        accruals.values("customer_user_id_snapshot")
        .annotate(total=Sum("partner_share"))
        .values("total")[:1]
    )
    counted = (
        accruals.values("customer_user_id_snapshot")
        .annotate(total=Count("pk"))
        .values("total")[:1]
    )
    return base.annotate(
        total_partner_earned=Coalesce(
            Subquery(earned, output_field=_MONEY),
            Value(_ZERO, output_field=_MONEY),
        ),
        accrual_count=Coalesce(
            Subquery(counted, output_field=IntegerField()),
            Value(0),
        ),
    )


def _customer_id_query(q: str) -> int | None:
    if _USER_ID_RE.fullmatch(q) is None:
        return None
    value = int(q)
    if value < 1 or value > _BIGINT_MAX:
        return None
    return value


def mask_partner_email(email: str) -> str:
    """First local character, ``***``, then the domain."""
    local, separator, domain = email.partition("@")
    if not local or not separator or not domain:
        return "***"
    return f"{local[0]}***@{domain}"


def _row(attribution: CustomerAttribution) -> PartnerCustomerRow:
    return PartnerCustomerRow(
        customer_id=attribution.user_id,
        email=mask_partner_email(attribution.user.email),
        attributed_at=attribution.attributed_at,
        total_partner_earned=attribution.total_partner_earned,
        accrual_count=attribution.accrual_count,
    )


partner_customers_service = PartnerCustomersService()
