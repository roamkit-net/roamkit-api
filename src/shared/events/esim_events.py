"""Domain events for eSIM lifecycle (ADR 014) and auto top-up."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from shared.events.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class EsimInstalled(DomainEvent):
    """Published when an eSIM is marked installed (client-attested)."""

    esim_id: str
    iccid: str
    user_id: str


@dataclass(frozen=True, kw_only=True)
class EsimActivated(DomainEvent):
    """Published when provider usage first reports ACTIVE."""

    esim_id: str
    iccid: str
    user_id: str
    purchase_to_activation_seconds: float | None


@dataclass(frozen=True, kw_only=True)
class EsimFirstUsageDetected(DomainEvent):
    """Published when usage first shows data consumption."""

    esim_id: str
    iccid: str
    user_id: str


@dataclass(frozen=True, kw_only=True)
class AutoTopupSucceeded(DomainEvent):
    """Published after auto top-up purchase fulfills (snapshot for handlers)."""

    event_version: int = 1
    policy_id: str
    topup_id: str
    package_id: str
    amount: Decimal
    remaining_count: int | None
    account_id: str
    esim_id: str
    created_at: datetime


@dataclass(frozen=True, kw_only=True)
class AutoTopupPausedFunds(DomainEvent):
    """Published when auto top-up pauses for insufficient funds (deposit CTA)."""

    event_version: int = 1
    policy_id: str
    account_id: str
    esim_id: str
    package_id: str
    amount_required: Decimal | None
    balance: Decimal | None
    deposit_url: str
    created_at: datetime


@dataclass(frozen=True, kw_only=True)
class AutoTopupPolicyCreated(DomainEvent):
    """Published when a user/admin creates an auto top-up policy."""

    event_version: int = 1
    policy_id: str
    account_id: str
    esim_id: str
    package_id: str
    enabled: bool
    status: str
    reason: str
    trigger_mode: str
    renew_mode: str
    version: int
    actor: str
    created_at: datetime


@dataclass(frozen=True, kw_only=True)
class AutoTopupPolicyUpdated(DomainEvent):
    """Published when a user/admin updates an auto top-up policy."""

    event_version: int = 1
    policy_id: str
    account_id: str
    esim_id: str
    package_id: str
    enabled: bool
    status: str
    reason: str
    trigger_mode: str
    renew_mode: str
    version: int
    actor: str
    created_at: datetime
