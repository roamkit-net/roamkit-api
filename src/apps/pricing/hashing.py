"""Deterministic hashing for pricing context / quote fingerprint (ADR 019)."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import TYPE_CHECKING

from apps.pricing.money import money_round

if TYPE_CHECKING:
    from apps.pricing.types import PricingContext


def _dec(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(money_round(value), "f")


def pricing_context_hash(ctx: PricingContext, *, flag_enabled: bool) -> str:
    """SHA-256 hex of canonical context fields used for the quote.

    Does not include net in public APIs; net is included here only when present
    because it affects floor outcomes (ops/debug integrity).
    """
    profile = ctx.profile
    profile_id = str(profile.pk) if profile is not None else ""
    profile_version = str(profile.version) if profile is not None else ""
    account_id = str(ctx.account.pk) if ctx.account is not None else ""
    # Exact timestamp ISO — callers that need stable hashes must pass same ts.
    ts = ctx.timestamp.isoformat()
    payload = "|".join(
        [
            "v1",
            "1" if flag_enabled else "0",
            account_id,
            profile_id,
            profile_version,
            _dec(ctx.list_price),
            _dec(ctx.net_price),
            ctx.order_type,
            ts,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
