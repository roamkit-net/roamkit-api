"""Canonical money rounding for pricing (ADR 019).

No other ``Decimal.quantize`` on pricing/spend paths — use ``money_round`` only.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_MONEY_QUANTUM = Decimal("0.01")


def money_round(amount: Decimal | int | str) -> Decimal:
    """Round to 2 decimal places with ROUND_HALF_UP (catalog USD convention)."""
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)
