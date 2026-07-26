"""LifecycleService — sole mutator of Esim.status (ADR 014)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.esims.exceptions import (
    InvalidLifecycleTransitionError,
    UnknownLifecycleEventTypeError,
)
from apps.esims.models import ActivationPolicy, Esim, EsimLifecycleEvent
from shared.events.esim_events import (
    EsimActivated,
    EsimFirstUsageDetected,
    EsimInstalled,
)
from shared.events.event_bus import event_bus
from shared.providers.esim import UsageDTO

logger = logging.getLogger(__name__)

SETUP_VERSION = "1"

CLIENT_EVENT_ALLOWLIST = frozenset(
    {
        "install.opened",
        "install.qr_rendered",
        "install.qr_zoomed",
        "install.apple_install_clicked",
        "install.manual_install_clicked",
        "install.completed",
        "install.roaming_checklist_viewed",
        "install.setup_confirmed",
        "install.setup_skipped",
    }
)

_ALLOWED: dict[str, frozenset[str]] = {
    Esim.Status.PURCHASED: frozenset(
        {
            Esim.Status.INSTALLATION_STARTED,
            Esim.Status.INSTALLED,
        }
    ),
    Esim.Status.INSTALLATION_STARTED: frozenset({Esim.Status.INSTALLED}),
    Esim.Status.INSTALLED: frozenset(
        {
            Esim.Status.ACTIVATED,
            Esim.Status.EXHAUSTED,
            Esim.Status.EXPIRED,
        }
    ),
    Esim.Status.ACTIVATED: frozenset(
        {
            Esim.Status.IN_USE,
            Esim.Status.EXHAUSTED,
            Esim.Status.EXPIRED,
        }
    ),
    Esim.Status.IN_USE: frozenset(
        {
            Esim.Status.EXHAUSTED,
            Esim.Status.EXPIRED,
        }
    ),
    Esim.Status.EXHAUSTED: frozenset({Esim.Status.EXPIRED}),
    Esim.Status.EXPIRED: frozenset(),
    Esim.Status.UNKNOWN: frozenset(
        {
            Esim.Status.PURCHASED,
            Esim.Status.INSTALLATION_STARTED,
            Esim.Status.INSTALLED,
            Esim.Status.ACTIVATED,
            Esim.Status.IN_USE,
            Esim.Status.EXHAUSTED,
            Esim.Status.EXPIRED,
        }
    ),
}

# Ordered paths used when provider jumps ahead of client-attested install.
_PATHS: dict[str, tuple[str, ...]] = {
    Esim.Status.INSTALLED: (Esim.Status.INSTALLED,),
    Esim.Status.ACTIVATED: (Esim.Status.INSTALLED, Esim.Status.ACTIVATED),
    Esim.Status.IN_USE: (
        Esim.Status.INSTALLED,
        Esim.Status.ACTIVATED,
        Esim.Status.IN_USE,
    ),
    Esim.Status.EXHAUSTED: (Esim.Status.EXHAUSTED,),
    Esim.Status.EXPIRED: (Esim.Status.EXPIRED,),
}


class LifecycleService:
    """Guarded transitions and lifecycle event persistence for owned eSIMs."""

    def create_purchased(
        self,
        *,
        user,
        order,
        iccid: str,
        lpa: str = "",
        matching_id: str = "",
        qrcode: str = "",
        qrcode_url: str = "",
        direct_apple_installation_url: str = "",
        manual_installation: str = "",
        qrcode_installation: str = "",
        installation_guide_url: str = "",
        activation_policy: str = ActivationPolicy.UNKNOWN,
    ) -> Esim:
        """Create an eSIM in ``purchased`` with purchase-time policy snapshot."""
        policy = activation_policy or ActivationPolicy.UNKNOWN
        if policy not in ActivationPolicy.values:
            policy = ActivationPolicy.UNKNOWN

        esim = Esim.objects.create(
            user=user,
            order=order,
            iccid=iccid,
            lpa=lpa,
            matching_id=matching_id,
            qrcode=qrcode,
            qrcode_url=qrcode_url,
            direct_apple_installation_url=direct_apple_installation_url,
            manual_installation=manual_installation,
            qrcode_installation=qrcode_installation,
            installation_guide_url=installation_guide_url,
            status=Esim.Status.PURCHASED,
            activation_policy=policy,
        )
        self._record_system_event(
            esim,
            event_type="system.purchased",
            idempotency_key=f"system.purchased:{esim.pk}",
            payload={"activation_policy": policy},
        )
        return esim

    def transition(self, esim: Esim, target: str, *, reason: str = "") -> Esim:
        """Move ``esim`` to ``target`` if the state machine allows it."""
        current = esim.status
        if current == target:
            return esim
        allowed = _ALLOWED.get(current, frozenset())
        if target not in allowed:
            raise InvalidLifecycleTransitionError(current, target)

        esim.status = target
        esim.save(update_fields=["status", "updated_at"])
        self._record_system_event(
            esim,
            event_type=f"system.status.{target}",
            idempotency_key=(
                f"system.status.{target}:{esim.pk}:{esim.updated_at.isoformat()}"
            ),
            payload={"from": current, "to": target, "reason": reason},
        )
        return esim

    def apply_provider_usage(self, esim: Esim, usage: UsageDTO) -> Esim:
        """Advance lifecycle from provider usage without downgrading on UNKNOWN."""
        provider_status = (usage.status or "").strip().upper()
        if provider_status == "UNKNOWN":
            self._record_provider_event(
                esim,
                event_type="provider.usage_unknown",
                idempotency_key=(
                    f"provider.usage_unknown:{esim.pk}:"
                    f"{esim.usage_synced_at or 'none'}"
                ),
                payload={"provider_status": provider_status},
            )
            return esim

        target: str | None = None
        if provider_status == "EXPIRED":
            target = Esim.Status.EXPIRED
        elif provider_status == "FINISHED":
            target = Esim.Status.EXHAUSTED
        elif provider_status == "ACTIVE":
            target = (
                Esim.Status.IN_USE
                if self._has_consumption(usage)
                else Esim.Status.ACTIVATED
            )

        if target is None:
            return esim

        previous = esim.status
        try:
            self._advance_toward(esim, target, reason=f"provider:{provider_status}")
        except InvalidLifecycleTransitionError:
            logger.info(
                "esim_lifecycle skip_transition esim_id=%s current=%s target=%s",
                esim.pk,
                esim.status,
                target,
            )
            return esim

        esim.refresh_from_db(fields=["status", "updated_at", "created_at"])
        if (
            previous != Esim.Status.ACTIVATED
            and esim.status in {Esim.Status.ACTIVATED, Esim.Status.IN_USE}
            and previous
            not in {
                Esim.Status.ACTIVATED,
                Esim.Status.IN_USE,
                Esim.Status.EXHAUSTED,
                Esim.Status.EXPIRED,
            }
        ):
            seconds = (timezone.now() - esim.created_at).total_seconds()
            logger.info(
                "esim_lifecycle activation_detected esim_id=%s "
                "purchase_to_activation_seconds=%s",
                esim.pk,
                seconds,
            )
            event_bus.publish(
                EsimActivated(
                    esim_id=str(esim.pk),
                    iccid=esim.iccid,
                    user_id=str(esim.user_id),
                    purchase_to_activation_seconds=seconds,
                )
            )
        if previous != Esim.Status.IN_USE and esim.status == Esim.Status.IN_USE:
            event_bus.publish(
                EsimFirstUsageDetected(
                    esim_id=str(esim.pk),
                    iccid=esim.iccid,
                    user_id=str(esim.user_id),
                )
            )
        return esim

    def record_client_event(
        self,
        esim: Esim,
        *,
        event_type: str,
        idempotency_key: str,
        schema_version: int = 1,
        setup_session_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
        user_agent: str = "",
        resume_step: int | None = None,
    ) -> tuple[EsimLifecycleEvent, bool]:
        """Persist a client event; return ``(event, created)``."""
        if event_type not in CLIENT_EVENT_ALLOWLIST:
            raise UnknownLifecycleEventTypeError(event_type)

        existing = EsimLifecycleEvent.objects.filter(
            esim=esim, idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            return existing, False

        with transaction.atomic():
            event = EsimLifecycleEvent.objects.create(
                esim=esim,
                user=esim.user,
                event_type=event_type,
                source=EsimLifecycleEvent.Source.CLIENT,
                schema_version=schema_version,
                idempotency_key=idempotency_key,
                setup_session_id=setup_session_id,
                payload=payload or {},
                user_agent=user_agent or "",
            )
            self._apply_client_side_effects(
                esim, event_type=event_type, resume_step=resume_step
            )
        return event, True

    def _apply_client_side_effects(
        self,
        esim: Esim,
        *,
        event_type: str,
        resume_step: int | None,
    ) -> None:
        update_fields: list[str] = []
        if resume_step is not None and 1 <= resume_step <= 4:
            esim.setup_resume_step = resume_step
            update_fields.append("setup_resume_step")

        if event_type == "install.opened":
            esim.setup_version = SETUP_VERSION
            update_fields.append("setup_version")
            if esim.status == Esim.Status.PURCHASED:
                self.transition(
                    esim, Esim.Status.INSTALLATION_STARTED, reason=event_type
                )
            logger.info("esim_lifecycle setup_started esim_id=%s", esim.pk)

        elif event_type == "install.completed":
            if esim.status in {
                Esim.Status.PURCHASED,
                Esim.Status.INSTALLATION_STARTED,
            }:
                self.transition(esim, Esim.Status.INSTALLED, reason=event_type)
            event_bus.publish(
                EsimInstalled(
                    esim_id=str(esim.pk),
                    iccid=esim.iccid,
                    user_id=str(esim.user_id),
                )
            )

        elif event_type == "install.setup_confirmed":
            if esim.setup_completed_at is None:
                esim.setup_completed_at = timezone.now()
                update_fields.append("setup_completed_at")
            logger.info("esim_lifecycle setup_completed esim_id=%s", esim.pk)

        elif event_type == "install.setup_skipped":
            if esim.setup_skipped_at is None:
                esim.setup_skipped_at = timezone.now()
                update_fields.append("setup_skipped_at")
            logger.info("esim_lifecycle setup_skipped esim_id=%s", esim.pk)

        if update_fields:
            update_fields.append("updated_at")
            esim.save(update_fields=update_fields)

    def _advance_toward(self, esim: Esim, target: str, *, reason: str) -> None:
        if esim.status == target:
            return

        if target in _ALLOWED.get(esim.status, frozenset()):
            self.transition(esim, target, reason=reason)
            return

        for step in _PATHS.get(target, (target,)):
            if esim.status == step:
                continue
            if step in _ALLOWED.get(esim.status, frozenset()):
                self.transition(esim, step, reason=reason)

        if esim.status != target:
            if target in _ALLOWED.get(esim.status, frozenset()):
                self.transition(esim, target, reason=reason)
            else:
                raise InvalidLifecycleTransitionError(esim.status, target)

    @staticmethod
    def _has_consumption(usage: UsageDTO) -> bool:
        if usage.is_unlimited:
            return False
        if usage.total_mb <= 0:
            return False
        return usage.remaining_mb < usage.total_mb

    def _record_system_event(
        self,
        esim: Esim,
        *,
        event_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> None:
        EsimLifecycleEvent.objects.get_or_create(
            esim=esim,
            idempotency_key=idempotency_key,
            defaults={
                "user": esim.user,
                "event_type": event_type,
                "source": EsimLifecycleEvent.Source.SYSTEM,
                "schema_version": 1,
                "payload": payload,
            },
        )

    def _record_provider_event(
        self,
        esim: Esim,
        *,
        event_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> None:
        EsimLifecycleEvent.objects.get_or_create(
            esim=esim,
            idempotency_key=idempotency_key,
            defaults={
                "user": esim.user,
                "event_type": event_type,
                "source": EsimLifecycleEvent.Source.PROVIDER,
                "schema_version": 1,
                "payload": payload,
            },
        )


lifecycle_service = LifecycleService()
