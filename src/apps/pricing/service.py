"""PricingService — sole product price calculator (ADR 019).

Does not touch CreditService / ledger. Callers persist ``PricingQuote`` snapshots;
debit must use snapshotted ``customer_price`` (PR3), never re-resolve.

Wholesale path uses margin-share: discount_percent is % of partner margin
(list − net) given to the customer, not % off retail.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from apps.pricing.hashing import pricing_context_hash
from apps.pricing.models import FloorPolicy, PricingProfile
from apps.pricing.money import money_round
from apps.pricing.types import (
    SNAPSHOT_SCHEMA_VERSION,
    FloorReason,
    PricingContext,
    PricingQuote,
    PricingReason,
)
from core import metrics

logger = logging.getLogger(__name__)

_HUNDRED = Decimal("100")
_ZERO = Decimal("0")


def _profile_is_effective(profile: PricingProfile, *, at) -> bool:
    if profile.archived_at is not None:
        return False
    if not profile.is_active:
        return False
    if profile.effective_from and at < profile.effective_from:
        return False
    if profile.effective_until is not None and at >= profile.effective_until:
        return False
    return True


def _retail_quote(
    *,
    list_price: Decimal,
    context_hash: str,
    discount_percent: Decimal = Decimal("0.00"),
    pricing_reason: str = PricingReason.RETAIL,
) -> PricingQuote:
    return PricingQuote(
        list_price=list_price,
        customer_price=list_price,
        discount_percent=money_round(discount_percent),
        pricing_reason=pricing_reason,
        floor_reason=FloorReason.NONE,
        pricing_profile_id=None,
        pricing_profile_version=None,
        pricing_context_hash=context_hash,
        profile_slug=None,
        profile_name=None,
        snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
    )


def _profile_quote(
    *,
    list_price: Decimal,
    customer_price: Decimal,
    discount: Decimal,
    pricing_reason: str,
    floor_reason: str,
    profile: PricingProfile,
    context_hash: str,
) -> PricingQuote:
    return PricingQuote(
        list_price=list_price,
        customer_price=customer_price,
        discount_percent=discount,
        pricing_reason=pricing_reason,
        floor_reason=floor_reason,
        pricing_profile_id=profile.pk,
        pricing_profile_version=profile.version,
        pricing_context_hash=context_hash,
        profile_slug=profile.slug,
        profile_name=profile.name,
        snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
    )


def _emit_catalog_signal(
    metric_name: str,
    *,
    message: str,
    list_price: Decimal,
    net_price: Decimal | None,
    profile: PricingProfile,
) -> None:
    """Warning log and metric must be emitted together (ADR 019)."""
    logger.warning(
        message,
        extra={
            "metric": metric_name,
            "list_price": str(list_price),
            "net_price": None if net_price is None else str(net_price),
            "profile_slug": profile.slug,
            "profile_id": str(profile.pk),
        },
    )
    metrics.incr(metric_name, profile_slug=profile.slug)


def _legacy_percent_off_list(
    *,
    list_price: Decimal,
    discount: Decimal,
) -> Decimal:
    """floor_policy=none — interim percent-off-list (legacy compatibility)."""
    raw = list_price * (_HUNDRED - discount) / _HUNDRED
    return money_round(raw)


def _margin_share_customer(
    *,
    list_price: Decimal,
    net_price: Decimal,
    discount: Decimal,
) -> Decimal:
    """Wholesale path: C = N + margin × (100 − D) / 100; round once on C."""
    margin = max(_ZERO, list_price - net_price)
    raw = net_price + margin * (_HUNDRED - discount) / _HUNDRED
    return money_round(raw)


class PricingService:
    """Resolve list → customer price for an account/context."""

    def resolve(self, ctx: PricingContext) -> PricingQuote:
        flag_enabled = bool(getattr(settings, "PRICING_PROFILES_ENABLED", False))
        list_price = money_round(ctx.list_price)
        at = ctx.timestamp or timezone.now()

        profile = ctx.profile
        if profile is None and ctx.account is not None:
            profile = getattr(ctx.account, "pricing_profile", None)

        # Hash effective inputs (resolved profile) for fingerprint stability.
        hash_ctx = replace(ctx, profile=profile, list_price=list_price)
        context_hash = pricing_context_hash(hash_ctx, flag_enabled=flag_enabled)

        if not flag_enabled:
            return _retail_quote(list_price=list_price, context_hash=context_hash)

        if profile is None or not _profile_is_effective(profile, at=at):
            return _retail_quote(list_price=list_price, context_hash=context_hash)

        discount = money_round(profile.discount_percent)

        # D=0 → retail charge; profile still recorded (compatibility).
        if discount <= 0:
            return _profile_quote(
                list_price=list_price,
                customer_price=list_price,
                discount=Decimal("0.00"),
                pricing_reason=PricingReason.PRICING_PROFILE,
                floor_reason=FloorReason.NONE,
                profile=profile,
                context_hash=context_hash,
            )

        # Legacy percent-off-list (not recommended for new profiles).
        if profile.floor_policy == FloorPolicy.NONE:
            customer = _legacy_percent_off_list(
                list_price=list_price, discount=discount
            )
            return _profile_quote(
                list_price=list_price,
                customer_price=customer,
                discount=discount,
                pricing_reason=PricingReason.PRICING_PROFILE,
                floor_reason=FloorReason.DISCOUNT,
                profile=profile,
                context_hash=context_hash,
            )

        # Wholesale margin-share (default).
        if ctx.net_price is None:
            _emit_catalog_signal(
                "pricing.net_missing",
                message=(
                    "pricing.net_missing: net_price absent; falling back to retail"
                ),
                list_price=list_price,
                net_price=None,
                profile=profile,
            )
            return _retail_quote(list_price=list_price, context_hash=context_hash)

        net_price = money_round(ctx.net_price)
        if list_price < net_price:
            _emit_catalog_signal(
                "pricing.invalid_margin",
                message=(
                    "pricing.invalid_margin: list_price < net_price; "
                    "falling back to retail"
                ),
                list_price=list_price,
                net_price=net_price,
                profile=profile,
            )
            return _retail_quote(
                list_price=list_price,
                context_hash=context_hash,
                pricing_reason=PricingReason.INVALID_MARGIN,
            )

        customer = _margin_share_customer(
            list_price=list_price,
            net_price=net_price,
            discount=discount,
        )
        return _profile_quote(
            list_price=list_price,
            customer_price=customer,
            discount=discount,
            pricing_reason=PricingReason.PRICING_PROFILE,
            floor_reason=FloorReason.DISCOUNT,
            profile=profile,
            context_hash=context_hash,
        )


pricing_service = PricingService()
