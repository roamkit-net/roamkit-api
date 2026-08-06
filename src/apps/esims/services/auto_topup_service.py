"""Auto top-up evaluation — eSIM domain; spend via TopupService (design lock)."""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.billing.exceptions import InsufficientFundsError
from apps.esims.exceptions import TopupPackageNotFoundError
from apps.esims.models import Esim, EsimAutoTopupPolicy
from apps.esims.services.topup_service import TopupService
from apps.esims.services.usage_service import UsageService
from apps.orders.exceptions import ProviderFulfillmentError, SpendInProgressError
from core import metrics
from shared.events.esim_events import (
    AutoTopupPausedFunds,
    AutoTopupPolicyCreated,
    AutoTopupPolicyUpdated,
    AutoTopupSucceeded,
)
from shared.events.event_bus import event_bus

if TYPE_CHECKING:
    from shared.providers.esim import TopupProvider

logger = logging.getLogger(__name__)


class AutoTopupService:
    """Evaluate enabled policies and purchase via ``TopupService`` when triggered."""

    def __init__(self, provider: TopupProvider) -> None:
        self.provider = provider
        self._usage = UsageService(provider)
        self._topups = TopupService(provider)

    def evaluate_due(self) -> dict[str, int]:
        """Process all enabled active policies (beat entrypoint)."""
        if not self._master_enabled():
            return {"disabled": 1}

        policy_ids = list(
            EsimAutoTopupPolicy.objects.filter(
                enabled=True,
                status=EsimAutoTopupPolicy.Status.ACTIVE,
            )
            .order_by("created_at")
            .values_list("pk", flat=True)
        )
        stats: dict[str, int] = {
            "success": 0,
            "skipped": 0,
            "paused": 0,
            "blocked": 0,
            "failed": 0,
        }
        for policy_id in policy_ids:
            outcome = self.evaluate_one(policy_id)
            stats[outcome] = stats.get(outcome, 0) + 1
        return stats

    def evaluate_one(self, policy_id) -> str:
        """Evaluate a single policy. Returns success|skipped|paused|blocked|failed."""
        started = time.monotonic()
        metrics.incr("auto_topup_attempts_total")
        try:
            outcome = self._evaluate_one_inner(policy_id)
        finally:
            metrics.observe(
                "auto_topup_duration_seconds",
                time.monotonic() - started,
            )
        return outcome

    def publish_policy_created(
        self,
        policy: EsimAutoTopupPolicy,
        *,
        actor: str = "system",
    ) -> None:
        """Snapshot event for policy create (called from API/admin in PR4+)."""
        event_bus.publish(self._policy_created_event(policy, actor=actor))

    def publish_policy_updated(
        self,
        policy: EsimAutoTopupPolicy,
        *,
        actor: str = "system",
    ) -> None:
        """Snapshot event for policy update (called from API/admin in PR4+)."""
        event_bus.publish(self._policy_updated_event(policy, actor=actor))

    def _evaluate_one_inner(self, policy_id) -> str:
        prepared = self._prepare(policy_id)
        if prepared is None or prepared == "skipped":
            return "skipped"
        if isinstance(prepared, str):
            return prepared

        esim, package_id, idempotency_key = prepared
        try:
            topup = self._topups.purchase(
                esim,
                package_id=package_id,
                idempotency_key=idempotency_key,
            )
        except InsufficientFundsError as exc:
            return self._pause_funds(policy_id, exc)
        except TopupPackageNotFoundError:
            return self._block_package(policy_id)
        except SpendInProgressError:
            metrics.incr("auto_topup_failed_total", reason="spend_in_progress")
            return "failed"
        except ProviderFulfillmentError:
            metrics.incr("auto_topup_failed_total", reason="provider_timeout")
            logger.warning(
                "Auto top-up policy %s provider failure; retry next beat",
                policy_id,
            )
            return "failed"

        return self._on_success(policy_id, topup, idempotency_key)

    def _prepare(self, policy_id) -> tuple[Esim, str, str] | str:
        """Lock policy, refresh usage, decide whether to buy.

        Returns ``(esim, package_id, idempotency_key)`` or an outcome string.
        """
        with transaction.atomic():
            try:
                policy = (
                    EsimAutoTopupPolicy.objects.select_for_update()
                    .select_related("account", "account__user", "esim")
                    .get(pk=policy_id)
                )
            except EsimAutoTopupPolicy.DoesNotExist:
                return "skipped"

            if not policy.enabled or policy.status != EsimAutoTopupPolicy.Status.ACTIVE:
                return "skipped"

            if not self._rollout_allows(policy):
                return "skipped"

            esim = policy.esim

        if not self._ensure_fresh_usage(esim):
            metrics.incr("auto_topup_failed_total", reason="usage_stale")
            return "skipped"

        with transaction.atomic():
            policy = (
                EsimAutoTopupPolicy.objects.select_for_update()
                .select_related("esim")
                .get(pk=policy_id)
            )
            if not policy.enabled or policy.status != EsimAutoTopupPolicy.Status.ACTIVE:
                return "skipped"

            esim = policy.esim
            esim.refresh_from_db()
            now = timezone.now()
            if policy.cooldown_until and policy.cooldown_until > now:
                return "skipped"
            if not self._past_minimum_age(esim, now=now):
                return "skipped"
            if not self._trigger_met(policy, esim, now=now):
                return "skipped"

            return (
                esim,
                policy.package_id,
                self._idempotency_key(policy, esim),
            )

    def _ensure_fresh_usage(self, esim: Esim) -> bool:
        """Refresh usage when stale/missing. Return False if buy must be skipped."""
        max_age = timedelta(seconds=int(settings.AUTO_TOPUP_USAGE_MAX_AGE_SECONDS))
        synced = esim.usage_synced_at
        now = timezone.now()
        if synced is not None and (now - synced) <= max_age:
            return True
        try:
            self._usage.get_usage(esim)
        except Exception:
            logger.exception("Usage refresh failed for esim %s", esim.pk)
            return False
        esim.refresh_from_db(
            fields=[
                "usage_remaining_mb",
                "usage_total_mb",
                "usage_status",
                "usage_is_unlimited",
                "usage_expired_at",
                "usage_synced_at",
            ]
        )
        synced = esim.usage_synced_at
        return synced is not None and (timezone.now() - synced) <= max_age

    @staticmethod
    def _past_minimum_age(esim: Esim, *, now) -> bool:
        min_age = timedelta(seconds=int(settings.AUTO_TOPUP_MINIMUM_AGE_SECONDS))
        anchor = esim.setup_completed_at or esim.created_at
        if anchor is None:
            return False
        return (now - anchor) >= min_age

    @staticmethod
    def _trigger_met(policy: EsimAutoTopupPolicy, esim: Esim, *, now) -> bool:
        mode = policy.trigger_mode
        if mode in (
            EsimAutoTopupPolicy.TriggerMode.USAGE_ZERO,
            EsimAutoTopupPolicy.TriggerMode.USAGE_THRESHOLD,
        ):
            if esim.usage_is_unlimited:
                return False
            remaining = esim.usage_remaining_mb
            if remaining is None:
                return False
            if mode == EsimAutoTopupPolicy.TriggerMode.USAGE_ZERO:
                return remaining <= 0
            threshold = policy.threshold_mb or 0
            return remaining < threshold

        if mode == EsimAutoTopupPolicy.TriggerMode.EXPIRY:
            if esim.status == Esim.Status.EXPIRED:
                return True
            if (esim.usage_status or "").upper() == "EXPIRED":
                return True
            if esim.usage_expired_at is not None and esim.usage_expired_at <= now:
                return True
            return False
        return False

    @staticmethod
    def _idempotency_key(policy: EsimAutoTopupPolicy, esim: Esim) -> str:
        if policy.trigger_mode == EsimAutoTopupPolicy.TriggerMode.EXPIRY:
            stamp = (
                esim.usage_expired_at.isoformat()
                if esim.usage_expired_at is not None
                else "unknown"
            )
            return f"auto-topup:{policy.pk}:expiry:{stamp}"
        stamp = (
            esim.usage_synced_at.isoformat()
            if esim.usage_synced_at is not None
            else "unknown"
        )
        return f"auto-topup:{policy.pk}:{policy.trigger_mode}:{stamp}"

    def _on_success(self, policy_id, topup, idempotency_key: str) -> str:
        with transaction.atomic():
            policy = EsimAutoTopupPolicy.objects.select_for_update().get(pk=policy_id)
            cooldown = timedelta(seconds=int(settings.AUTO_TOPUP_COOLDOWN_SECONDS))
            now = timezone.now()
            policy.cooldown_until = now + cooldown
            policy.last_triggered_at = now
            policy.last_topup = topup
            policy.last_idempotency_key = idempotency_key
            update_fields = [
                "cooldown_until",
                "last_triggered_at",
                "last_topup",
                "last_idempotency_key",
                "updated_at",
            ]

            remaining_after: int | None = policy.remaining_count
            if policy.renew_mode == EsimAutoTopupPolicy.RenewMode.FIXED_COUNT:
                current = policy.remaining_count or 0
                remaining_after = max(current - 1, 0)
                policy.remaining_count = remaining_after
                update_fields.append("remaining_count")
                if remaining_after == 0:
                    policy.status = EsimAutoTopupPolicy.Status.PAUSED
                    policy.reason = EsimAutoTopupPolicy.Reason.COUNT_EXHAUSTED
                    update_fields.extend(["status", "reason"])
                    metrics.incr("auto_topup_paused_total", reason="count_exhausted")

            policy.save(update_fields=update_fields)
            event_bus.publish(
                AutoTopupSucceeded(
                    policy_id=str(policy.pk),
                    topup_id=str(topup.pk),
                    package_id=policy.package_id,
                    amount=topup.amount,
                    remaining_count=remaining_after,
                    account_id=str(policy.account_id),
                    esim_id=str(policy.esim_id),
                    created_at=now,
                )
            )
        metrics.incr("auto_topup_success_total")
        return "success"

    def _pause_funds(self, policy_id, exc: InsufficientFundsError) -> str:
        with transaction.atomic():
            policy = EsimAutoTopupPolicy.objects.select_for_update().get(pk=policy_id)
            policy.status = EsimAutoTopupPolicy.Status.PAUSED
            policy.reason = EsimAutoTopupPolicy.Reason.INSUFFICIENT_FUNDS
            policy.save(update_fields=["status", "reason", "updated_at"])
            event_bus.publish(
                AutoTopupPausedFunds(
                    policy_id=str(policy.pk),
                    account_id=str(policy.account_id),
                    esim_id=str(policy.esim_id),
                    package_id=policy.package_id,
                    amount_required=exc.amount_required,
                    balance=exc.account_balance,
                    deposit_url=self._deposit_url(),
                    created_at=timezone.now(),
                )
            )
        metrics.incr("auto_topup_paused_total", reason="insufficient_funds")
        return "paused"

    def _block_package(self, policy_id) -> str:
        with transaction.atomic():
            policy = EsimAutoTopupPolicy.objects.select_for_update().get(pk=policy_id)
            policy.status = EsimAutoTopupPolicy.Status.BLOCKED
            policy.reason = EsimAutoTopupPolicy.Reason.PACKAGE_UNAVAILABLE
            policy.save(update_fields=["status", "reason", "updated_at"])
        metrics.incr("auto_topup_paused_total", reason="package_unavailable")
        return "blocked"

    def _rollout_allows(self, policy: EsimAutoTopupPolicy) -> bool:
        if not self._master_enabled():
            return False
        mode = str(settings.AUTO_TOPUP_ROLLOUT_MODE).lower()
        if mode in ("", "off"):
            return False
        if mode == "all":
            return True
        user = policy.account.user
        if mode == "staff":
            return bool(user.is_staff)
        if mode == "allowlist":
            allow = {str(x) for x in settings.AUTO_TOPUP_ALLOWLIST_ACCOUNT_IDS}
            return str(policy.account_id) in allow
        if mode == "percent":
            percent = int(settings.AUTO_TOPUP_ROLLOUT_PERCENT)
            if percent <= 0:
                return False
            if percent >= 100:
                return True
            digest = hashlib.sha256(str(policy.account_id).encode()).hexdigest()
            bucket = int(digest[:8], 16) % 100
            return bucket < percent
        return False

    @staticmethod
    def _master_enabled() -> bool:
        return bool(settings.AUTO_TOPUP_ENABLED) and bool(settings.BILLING_ENABLED)

    @staticmethod
    def _deposit_url() -> str:
        base = str(settings.FRONTEND_BASE_URL).rstrip("/")
        return f"{base}/me/deposit"

    @staticmethod
    def _policy_created_event(
        policy: EsimAutoTopupPolicy, *, actor: str
    ) -> AutoTopupPolicyCreated:
        return AutoTopupPolicyCreated(
            policy_id=str(policy.pk),
            account_id=str(policy.account_id),
            esim_id=str(policy.esim_id),
            package_id=policy.package_id,
            enabled=policy.enabled,
            status=policy.status,
            reason=policy.reason or "",
            trigger_mode=policy.trigger_mode,
            renew_mode=policy.renew_mode,
            version=policy.version,
            actor=actor,
            created_at=timezone.now(),
        )

    @staticmethod
    def _policy_updated_event(
        policy: EsimAutoTopupPolicy, *, actor: str
    ) -> AutoTopupPolicyUpdated:
        return AutoTopupPolicyUpdated(
            policy_id=str(policy.pk),
            account_id=str(policy.account_id),
            esim_id=str(policy.esim_id),
            package_id=policy.package_id,
            enabled=policy.enabled,
            status=policy.status,
            reason=policy.reason or "",
            trigger_mode=policy.trigger_mode,
            renew_mode=policy.renew_mode,
            version=policy.version,
            actor=actor,
            created_at=timezone.now(),
        )
