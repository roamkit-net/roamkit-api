"""Public / internal pricing presentation helpers (ADR 019 PR4).

Public catalog exposes only customer-facing fields. Internal preview may return
ops fields (fingerprint, floor_reason, profile ids). Never call CreditService.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.utils import timezone

from apps.pricing.service import pricing_service
from apps.pricing.types import OrderType, PricingContext, PricingQuote

if TYPE_CHECKING:
    from apps.billing.models import Account
    from apps.catalog.models import Package
    from shared.providers.esim import TopupPackage


# Fields allowed on public catalog / topup list responses.
PUBLIC_PRICE_FIELDS = (
    "price_usd",
    "list_price_usd",
    "discount_percent",
    "pricing_reason",
)

# Substrings / names that must never appear on public API serializers.
PUBLIC_LEAK_FORBIDDEN_EXACT = frozenset(
    {
        "cost",
        "net_price",
        "net_price_usd",
        "margin",
        "provider_cost",
        "wholesale",
        "pricing_profile_id",
        "pricing_profile_version",
        "pricing_context_hash",
        "quote_fingerprint",
        "floor_reason",
    }
)
PUBLIC_LEAK_FORBIDDEN_SUBSTRINGS = (
    "net_price",
    "net_price_usd",
    "margin",
    "provider_cost",
    "wholesale",
    "pricing_profile_id",
    "pricing_profile_version",
    "pricing_context_hash",
    "quote_fingerprint",
    "floor_reason",
)


def public_price_dict(quote: PricingQuote) -> dict[str, Any]:
    """Map a quote to additive public API price fields."""
    return {
        "price_usd": quote.customer_price,
        "list_price_usd": quote.list_price,
        "discount_percent": quote.discount_percent,
        "pricing_reason": quote.pricing_reason,
    }


def internal_preview_dict(quote: PricingQuote) -> dict[str, Any]:
    """Ops preview payload — includes fingerprint; still no ledger writes."""
    return {
        **public_price_dict(quote),
        "floor_reason": quote.floor_reason,
        "pricing_profile_id": (
            str(quote.pricing_profile_id) if quote.pricing_profile_id else None
        ),
        "pricing_profile_version": quote.pricing_profile_version,
        "pricing_profile_slug": quote.profile_slug,
        "pricing_context_hash": quote.pricing_context_hash,
        "quote_fingerprint": quote.fingerprint,
        "snapshot_schema_version": quote.snapshot_schema_version,
    }


def resolve_package_quote(
    package: Package, *, account: Account | None, at=None
) -> PricingQuote:
    """Resolve catalog package price for display (same engine as charge path)."""
    return pricing_service.resolve(
        PricingContext(
            list_price=package.price_usd,
            net_price=getattr(package, "net_price_usd", None),
            order_type=OrderType.PACKAGE,
            timestamp=at or timezone.now(),
            account=account,
            profile=getattr(account, "pricing_profile", None) if account else None,
        )
    )


def resolve_topup_quote(
    package: TopupPackage, *, account: Account | None, at=None
) -> PricingQuote:
    """Resolve top-up package price for display."""
    return pricing_service.resolve(
        PricingContext(
            list_price=package.price_usd,
            net_price=getattr(package, "net_price_usd", None),
            order_type=OrderType.TOPUP,
            timestamp=at or timezone.now(),
            account=account,
            profile=getattr(account, "pricing_profile", None) if account else None,
        )
    )


def resolve_preview_quote(
    *,
    list_price: Decimal,
    net_price: Decimal | None,
    order_type: str,
    account: Account | None,
    at=None,
) -> PricingQuote:
    """Internal preview — thin wrapper over ``PricingService.resolve`` only."""
    return pricing_service.resolve(
        PricingContext(
            list_price=list_price,
            net_price=net_price,
            order_type=order_type,
            timestamp=at or timezone.now(),
            account=account,
            profile=getattr(account, "pricing_profile", None) if account else None,
        )
    )


def pricing_account_for_request(request) -> Account | None:
    """Billing account for authenticated users; None for anonymous."""
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    from apps.billing.models import Account
    from apps.billing.services import ensure_billing_account

    account = ensure_billing_account(user)
    return Account.objects.select_related("pricing_profile").get(pk=account.pk)
