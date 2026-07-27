"""Billing domain events (snapshot payloads — ADR-010)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
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


@dataclass(frozen=True, kw_only=True)
class CreditDebited(DomainEvent):
    """Published after credits are debited for an order / top-up spend.

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


@dataclass(frozen=True, kw_only=True)
class FulfillmentRefunded(DomainEvent):
    """Published after provider failure triggers a compensating REFUND credit.

    Snapshot fields cover the refund without re-fetching ledger or order/top-up.
    """

    event_version: int = 1
    account_id: str
    amount: Decimal
    balance_after: Decimal
    reference_type: str
    reference_id: str
    ledger_entry_id: str
    reason: str
    created_at: datetime


@dataclass(frozen=True, kw_only=True)
class SubscriptionRenewed(DomainEvent):
    """Published after subscription debit; next_billing_date advanced."""

    event_version: int = 1
    subscription_id: str
    account_id: str
    esim_id: str
    amount: Decimal
    balance_after: Decimal
    next_billing_date: date
    ledger_entry_id: str
    created_at: datetime


@dataclass(frozen=True, kw_only=True)
class SubscriptionPaused(DomainEvent):
    """Published when renewal fails for insufficient funds (email → /me/deposit)."""

    event_version: int = 1
    subscription_id: str
    account_id: str
    esim_id: str
    amount_required: Decimal
    balance: Decimal
    deposit_url: str
    next_billing_date: date
    created_at: datetime


@dataclass(frozen=True, kw_only=True)
class BalanceDriftDetected(DomainEvent):
    """Published when Account.balance cache differs from ledger SUM (ops alert)."""

    event_version: int = 1
    account_id: str
    cached_balance: Decimal
    ledger_sum: Decimal
    drift: Decimal
    detected_at: datetime


@dataclass(frozen=True, kw_only=True)
class VoucherRedeemed(DomainEvent):
    """Published after a voucher or shared campaign code credits an account.

    Snapshot fields cover happy-path handlers — no redemption re-fetch.
    ``request_id`` correlates HTTP → redemption → ledger → logs (ADR 011).
    """

    event_version: int = 1
    voucher_id: str | None
    campaign_id: str | None
    redemption_id: str
    account_id: str
    amount: Decimal
    balance_after: Decimal
    ledger_entry_id: str
    request_id: str
    redeemed_at: datetime
