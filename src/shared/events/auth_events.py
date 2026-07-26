"""Auth domain events (Google OAuth — ADR 015)."""

from __future__ import annotations

from dataclasses import dataclass

from shared.events.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class GoogleLoginSucceeded(DomainEvent):
    event_version: int = 1
    user_id: int
    google_sub: str


@dataclass(frozen=True, kw_only=True)
class GoogleLoginFailed(DomainEvent):
    event_version: int = 1
    reason: str
    google_sub: str | None = None


@dataclass(frozen=True, kw_only=True)
class GoogleAccountLinked(DomainEvent):
    event_version: int = 1
    user_id: int
    google_sub: str


@dataclass(frozen=True, kw_only=True)
class GoogleAccountCreated(DomainEvent):
    event_version: int = 1
    user_id: int
    google_sub: str


@dataclass(frozen=True, kw_only=True)
class GoogleLoginConflict(DomainEvent):
    event_version: int = 1
    google_sub: str
