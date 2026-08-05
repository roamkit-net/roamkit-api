"""Once-per-purchase charge resolution (ADR 019 PR3).

Callers must invoke these helpers **before** persisting the Order/Topup snapshot,
then debit/refund/replay exclusively from the snapshotted charged amount.
Never call ``PricingService.resolve`` again for the same spend row.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.utils import timezone

from apps.pricing.service import pricing_service
from apps.pricing.snapshot import (
    quote_to_order_snapshot_kwargs,
    quote_to_topup_snapshot_kwargs,
)
from apps.pricing.types import OrderType, PricingContext

if TYPE_CHECKING:
    from apps.billing.models import Account
    from apps.catalog.models import Package
    from shared.providers.esim import TopupPackage


def resolve_package_charge(
    *, account: Account, package: Package
) -> tuple[Decimal, dict[str, Any]]:
    """Return ``(charge_amount, pricing_snapshot_kwargs)`` for a catalog package.

    When ``PRICING_PROFILES_ENABLED`` is false, charge is legacy ``package.price_usd``
    and pricing snapshot kwargs are empty (product snapshot still sets retail).
    """
    if not settings.PRICING_PROFILES_ENABLED:
        return package.price_usd, {}

    account = type(account).objects.select_related("pricing_profile").get(pk=account.pk)
    quote = pricing_service.resolve(
        PricingContext(
            list_price=package.price_usd,
            net_price=getattr(package, "net_price_usd", None),
            order_type=OrderType.PACKAGE,
            timestamp=timezone.now(),
            account=account,
            profile=account.pricing_profile,
        )
    )
    return quote.customer_price, quote_to_order_snapshot_kwargs(quote)


def resolve_topup_charge(
    *, account: Account, package: TopupPackage
) -> tuple[Decimal, dict[str, Any]]:
    """Return ``(charge_amount, pricing_snapshot_kwargs)`` for a top-up package."""
    if not settings.PRICING_PROFILES_ENABLED:
        return package.price_usd, {}

    account = type(account).objects.select_related("pricing_profile").get(pk=account.pk)
    quote = pricing_service.resolve(
        PricingContext(
            list_price=package.price_usd,
            net_price=getattr(package, "net_price_usd", None),
            order_type=OrderType.TOPUP,
            timestamp=timezone.now(),
            account=account,
            profile=account.pricing_profile,
        )
    )
    return quote.customer_price, quote_to_topup_snapshot_kwargs(quote)
