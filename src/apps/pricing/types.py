"""Pricing value objects (ADR 019)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from apps.billing.models import Account
    from apps.pricing.models import PricingProfile


class OrderType:
    PACKAGE = "package"
    TOPUP = "topup"


class PricingReason:
    RETAIL = "retail"
    PRICING_PROFILE = "pricing_profile"
    INVALID_MARGIN = "invalid_margin"


class FloorReason:
    NONE = "none"
    DISCOUNT = "discount"
    WHOLESALE_FLOOR = "wholesale_floor"


SNAPSHOT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PricingContext:
    """Input to ``PricingService.resolve`` — extend fields carefully (ADR 019)."""

    list_price: Decimal
    net_price: Decimal | None
    order_type: str
    timestamp: datetime
    account: Account | None = None
    profile: PricingProfile | None = None


@dataclass(frozen=True)
class PricingQuote:
    """Immutable resolve result. Persist via snapshot; never re-resolve for debit."""

    list_price: Decimal
    customer_price: Decimal
    discount_percent: Decimal
    pricing_reason: str
    floor_reason: str
    pricing_profile_id: UUID | None
    pricing_profile_version: int | None
    pricing_context_hash: str
    profile_slug: str | None
    profile_name: str | None
    snapshot_schema_version: int = SNAPSHOT_SCHEMA_VERSION

    @property
    def fingerprint(self) -> str:
        """Stable debug identity: profile id + version + context hash."""
        pid = str(self.pricing_profile_id) if self.pricing_profile_id else "-"
        ver = (
            str(self.pricing_profile_version)
            if self.pricing_profile_version is not None
            else "-"
        )
        return f"{pid}|{ver}|{self.pricing_context_hash}"
