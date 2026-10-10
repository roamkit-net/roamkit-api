"""Read-only customer eSIM plans for one partner channel (ADR 024).

The caller passes an already authorized channel. This module does not refresh
usage, call a provider, or create an Account.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.core.exceptions import ObjectDoesNotExist

from apps.billing.partner_channel import CustomerAttribution, PartnerChannel
from apps.esims.models import Esim


@dataclass(frozen=True, slots=True)
class PartnerCustomerPlan:
    location_title: str
    package_title: str
    data_allowance: str
    validity_days: int | None
    status: str
    usage_remaining_mb: int | None
    usage_total_mb: int | None
    usage_is_unlimited: bool | None
    usage_expired_at: datetime | None
    usage_synced_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PartnerCustomerPlans:
    active: tuple[PartnerCustomerPlan, ...]
    expired: tuple[PartnerCustomerPlan, ...]


_EMPTY = PartnerCustomerPlans(active=(), expired=())


class PartnerCustomerPlansService:
    """Local eSIM cache for the customer's personal Account."""

    def list_plans(
        self,
        channel: PartnerChannel,
        customer_id: int,
    ) -> PartnerCustomerPlans | None:
        """Return plans, or None when this channel has no current attribution."""
        attribution = (
            CustomerAttribution.objects.filter(
                partner_channel=channel,
                user_id=customer_id,
            )
            .select_related("user__billing_account")
            .first()
        )
        if attribution is None:
            return None
        try:
            account = attribution.user.billing_account
        except ObjectDoesNotExist:
            return _EMPTY
        esims = Esim.objects.filter(
            account_id=account.pk,
            archived_at__isnull=True,
        ).select_related("order")
        active: list[Esim] = []
        expired: list[Esim] = []
        for esim in esims:
            if esim.status == Esim.Status.EXPIRED:
                expired.append(esim)
            else:
                active.append(esim)
        active.sort(key=_active_sort_key)
        expired.sort(key=_expired_sort_key)
        return PartnerCustomerPlans(
            active=tuple(_plan(esim) for esim in active),
            expired=tuple(_plan(esim) for esim in expired),
        )


def _plan(esim: Esim) -> PartnerCustomerPlan:
    order = esim.order
    return PartnerCustomerPlan(
        location_title=order.location_title,
        package_title=order.package_title,
        data_allowance=order.data_allowance,
        validity_days=order.validity_days,
        status=esim.status,
        usage_remaining_mb=esim.usage_remaining_mb,
        usage_total_mb=esim.usage_total_mb,
        usage_is_unlimited=esim.usage_is_unlimited,
        usage_expired_at=esim.usage_expired_at,
        usage_synced_at=esim.usage_synced_at,
        created_at=esim.created_at,
    )


def _active_sort_key(esim: Esim) -> tuple[float, int]:
    return (-esim.created_at.timestamp(), -esim.pk)


def _expired_sort_key(esim: Esim) -> tuple[bool, float, float, int]:
    expired_at = esim.usage_expired_at
    return (
        expired_at is None,
        -(expired_at.timestamp() if expired_at is not None else 0.0),
        -esim.created_at.timestamp(),
        -esim.pk,
    )


partner_customer_plans_service = PartnerCustomerPlansService()
