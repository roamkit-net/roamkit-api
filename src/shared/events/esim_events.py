"""Domain events for eSIM lifecycle (ADR 014)."""

from dataclasses import dataclass

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
