"""Billing domain events (snapshot payloads — ADR-010)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from shared.events.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class DepositVerified(DomainEvent):
    """Published after a deposit is verified and credited.

    Snapshot fields are enough for happy-path handlers — no deposit re-fetch.
    """

    event_version: int = 1
    deposit_id: str
    account_id: str
    amount: Decimal
    balance_after: Decimal
    tx_hash: str
    payment_method: str
    ledger_entry_id: str
    verified_at: datetime


@dataclass(frozen=True, kw_only=True)
class CreditGranted(DomainEvent):
    """Published after credits are added to an account.

    Snapshot fields are enough for happy-path handlers — no ledger re-fetch.
    """

    event_version: int = 1
    account_id: str
    amount: Decimal
    balance_after: Decimal
    reference_type: str
    reference_id: str
    ledger_entry_id: str
    created_at: datetime
