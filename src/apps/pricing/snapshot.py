"""Quote ↔ Order/Topup snapshot field mapping (ADR 019).

PR2 provides helpers only — OrderService/TopupService charge wiring is PR3.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from apps.pricing.money import money_round
from apps.pricing.types import SNAPSHOT_SCHEMA_VERSION, PricingQuote


def quote_to_order_snapshot_kwargs(quote: PricingQuote) -> dict[str, Any]:
    """Map quote onto ``Order`` pricing snapshot columns (not product fields)."""
    return {
        "list_price_usd": quote.list_price,
        "retail_price_usd": quote.customer_price,
        "discount_percent": quote.discount_percent,
        "pricing_reason": quote.pricing_reason,
        "floor_reason": quote.floor_reason,
        "pricing_profile_id": quote.pricing_profile_id,
        "pricing_profile_version": quote.pricing_profile_version,
        "pricing_profile_slug": quote.profile_slug or "",
        "pricing_profile_name": quote.profile_name or "",
        "pricing_context_hash": quote.pricing_context_hash,
        "snapshot_schema_version": quote.snapshot_schema_version,
    }


def quote_to_topup_snapshot_kwargs(quote: PricingQuote) -> dict[str, Any]:
    """Fields to set on ``Topup`` from a resolve quote."""
    return {
        "list_price_usd": quote.list_price,
        "amount": quote.customer_price,
        "discount_percent": quote.discount_percent,
        "pricing_reason": quote.pricing_reason,
        "floor_reason": quote.floor_reason,
        "pricing_profile_id": quote.pricing_profile_id,
        "pricing_profile_version": quote.pricing_profile_version,
        "pricing_profile_slug": quote.profile_slug or "",
        "pricing_profile_name": quote.profile_name or "",
        "pricing_context_hash": quote.pricing_context_hash,
        "snapshot_schema_version": quote.snapshot_schema_version,
    }


def quote_from_snapshot(
    *,
    list_price_usd: Decimal | None,
    customer_price: Decimal | None,
    discount_percent: Decimal | None,
    pricing_reason: str | None,
    floor_reason: str | None,
    pricing_profile_id: UUID | None,
    pricing_profile_version: int | None,
    pricing_profile_slug: str | None,
    pricing_profile_name: str | None,
    pricing_context_hash: str | None,
    snapshot_schema_version: int | None,
) -> PricingQuote:
    """Rebuild a ``PricingQuote`` from persisted snapshot columns."""
    if list_price_usd is None or customer_price is None:
        raise ValueError("list_price_usd and customer_price are required")
    if not pricing_context_hash:
        raise ValueError("pricing_context_hash is required")
    return PricingQuote(
        list_price=money_round(list_price_usd),
        customer_price=money_round(customer_price),
        discount_percent=money_round(discount_percent or Decimal("0.00")),
        pricing_reason=pricing_reason or "retail",
        floor_reason=floor_reason or "none",
        pricing_profile_id=pricing_profile_id,
        pricing_profile_version=pricing_profile_version,
        pricing_context_hash=pricing_context_hash,
        profile_slug=pricing_profile_slug or None,
        profile_name=pricing_profile_name or None,
        snapshot_schema_version=snapshot_schema_version or SNAPSHOT_SCHEMA_VERSION,
    )


def quote_from_order(order: Any) -> PricingQuote:
    return quote_from_snapshot(
        list_price_usd=order.list_price_usd,
        customer_price=order.retail_price_usd,
        discount_percent=order.discount_percent,
        pricing_reason=order.pricing_reason,
        floor_reason=order.floor_reason,
        pricing_profile_id=order.pricing_profile_id,
        pricing_profile_version=order.pricing_profile_version,
        pricing_profile_slug=order.pricing_profile_slug or None,
        pricing_profile_name=order.pricing_profile_name or None,
        pricing_context_hash=order.pricing_context_hash or None,
        snapshot_schema_version=order.snapshot_schema_version,
    )


def quote_from_topup(topup: Any) -> PricingQuote:
    return quote_from_snapshot(
        list_price_usd=topup.list_price_usd,
        customer_price=topup.amount,
        discount_percent=topup.discount_percent,
        pricing_reason=topup.pricing_reason,
        floor_reason=topup.floor_reason,
        pricing_profile_id=topup.pricing_profile_id,
        pricing_profile_version=topup.pricing_profile_version,
        pricing_profile_slug=topup.pricing_profile_slug or None,
        pricing_profile_name=topup.pricing_profile_name or None,
        pricing_context_hash=topup.pricing_context_hash or None,
        snapshot_schema_version=topup.snapshot_schema_version,
    )
