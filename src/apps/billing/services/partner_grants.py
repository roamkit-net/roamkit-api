"""Read-only partner grant history (ADR 023).

The caller passes an already resolved ``PartnerChannel`` and validated query
parameters. ``customer_id`` is ``customer_user_id_snapshot``. The live user is
used only to mask the customer email, and only while that user still exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from apps.billing.partner_channel import PartnerChannel, PartnerCreditGrant
from apps.billing.services.partner_customers import mask_partner_email

_SORT_FIELDS = {
    "created_at": "created_at",
    "amount": "amount",
}


@dataclass(frozen=True, slots=True)
class PartnerGrantsQuery:
    """Already validated list parameters. There is no ``q``."""

    page: int
    page_size: int
    sort: str
    order: str


@dataclass(frozen=True, slots=True)
class PartnerGrantActor:
    user_id: int
    email: str


@dataclass(frozen=True, slots=True)
class PartnerGrantHistoryRow:
    grant_id: UUID
    customer_id: int
    email: str | None
    amount: Decimal
    granted_by: PartnerGrantActor | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PartnerGrantsPage:
    count: int
    page: int
    page_size: int
    results: tuple[PartnerGrantHistoryRow, ...]


class PartnerGrantsService:
    """List stored grants for one channel. No locks or writes."""

    def list_grants(
        self,
        partner_channel: PartnerChannel,
        query: PartnerGrantsQuery,
    ) -> PartnerGrantsPage:
        base = PartnerCreditGrant.objects.filter(partner_channel=partner_channel)
        count = base.count()
        direction = "-" if query.order == "desc" else ""
        start = (query.page - 1) * query.page_size
        rows = base.select_related("customer_user", "granted_by").order_by(
            f"{direction}{_SORT_FIELDS[query.sort]}",
            "id",
        )[start : start + query.page_size]
        return PartnerGrantsPage(
            count=count,
            page=query.page,
            page_size=query.page_size,
            results=tuple(_row(item) for item in rows),
        )


def _row(grant: PartnerCreditGrant) -> PartnerGrantHistoryRow:
    email = None
    if grant.customer_user_id is not None and grant.customer_user is not None:
        email = mask_partner_email(grant.customer_user.email)
    actor = None
    if grant.granted_by_id is not None and grant.granted_by is not None:
        actor = PartnerGrantActor(
            user_id=grant.granted_by_user_id_snapshot,
            email=mask_partner_email(grant.granted_by.email),
        )
    return PartnerGrantHistoryRow(
        grant_id=grant.id,
        customer_id=grant.customer_user_id_snapshot,
        email=email,
        amount=grant.amount,
        granted_by=actor,
        created_at=grant.created_at,
    )


partner_grants_service = PartnerGrantsService()
