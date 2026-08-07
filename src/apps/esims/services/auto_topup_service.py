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
from apps.esims.models import Esim, EsimAutoTopupPolicy, Topup
from apps.esims.services.topup_service import TopupService
from apps.esims.services.usage_service import UsageService
from apps.orders.exceptions import ProviderFulfillmentError, SpendInProgressError
from core import metrics
from shared.events.esim_events import (
    AutoTopupConfigurationChanged,
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
        self._metric_incr("auto_topup_attempts_total")
        try:
            return self._evaluate_one_inner(policy_id)
        finally:
            self._metric_observe(
                "auto_topup_duration_seconds",
                time.monotonic() - started,
            )

    def publish_policy_created(
        self,
        policy: EsimAutoTopupPolicy,
        *,
        actor: str = "system",
    ) -> None:
        """Snapshot event for policy create (called from API/admin in PR4+)."""
        self._safe_publish(self._policy_created_event(policy, actor=actor))

    def publish_policy_updated(
        self,
        policy: EsimAutoTopupPolicy,
        *,
        actor: str = "system",
    ) -> None:
        """Snapshot event for policy update (called from API/admin in PR4+)."""
        self._safe_publish(self._policy_updated_event(policy, actor=actor))

    def _evaluate_one_inner(self, policy_id) -> str:
        prepared = self._prepare(policy_id)
        if prepared is None or prepared == "skipped":
            return "skipped"
        if isinstance(prepared, str):
            return prepared

        esim, package_id, idempotency_key, reason = prepared
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
            self._metric_incr("auto_topup_failed_total", reason="spend_in_progress")
            return "failed"
        except ProviderFulfillmentError:
            self._metric_incr("auto_topup_failed_total", reason="provider_timeout")
            logger.warning(
                "Auto top-up policy %s provider failure; retry next beat",
                policy_id,
            )
            return "failed"

        return self._on_success(policy_id, topup, idempotency_key, fire_reason=reason)

    def _prepare(self, policy_id) -> tuple[Esim, str, str, str] | str:
        """Lock policy, refresh usage, decide whether to buy.

        Returns ``(esim, package_id, idempotency_key, fire_reason)`` or an outcome
        string.
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
            self._metric_incr("auto_topup_failed_total", reason="usage_stale")
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

            reason = self._select_fire_reason(policy, esim, now=now)
            if reason is None:
                return "skipped"

            return (
                esim,
                policy.package_id,
                self._idempotency_key(policy, esim, reason=reason),
                reason,
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
    def _expiry_met(esim: Esim, *, now) -> bool:
        if esim.status == Esim.Status.EXPIRED:
            return True
        if (esim.usage_status or "").upper() == "EXPIRED":
            return True
        if esim.usage_expired_at is not None and esim.usage_expired_at <= now:
            return True
        return False

    @classmethod
    def _select_fire_reason(
        cls, policy: EsimAutoTopupPolicy, esim: Esim, *, now
    ) -> str | None:
        """OR evaluation: expiry first, then usage (v2). No pending queue."""
        if policy.expiry_enabled and cls._expiry_met(esim, now=now):
            return EsimAutoTopupPolicy.LEGACY_TRIGGER_EXPIRY

        if policy.usage_mode == EsimAutoTopupPolicy.UsageMode.THRESHOLD:
            if esim.usage_is_unlimited:
                return None
            remaining = esim.usage_remaining_mb
            if remaining is None:
                return None
            threshold = policy.threshold_mb or 0
            if remaining < threshold:
                return EsimAutoTopupPolicy.LEGACY_TRIGGER_USAGE_THRESHOLD
            return None

        if policy.usage_mode == EsimAutoTopupPolicy.UsageMode.ZERO:
            if esim.usage_is_unlimited:
                return None
            remaining = esim.usage_remaining_mb
            if remaining is None:
                return None
            if remaining <= 0:
                return EsimAutoTopupPolicy.LEGACY_TRIGGER_USAGE_ZERO
            return None

        return None

    @classmethod
    def _trigger_met(cls, policy: EsimAutoTopupPolicy, esim: Esim, *, now) -> bool:
        return cls._select_fire_reason(policy, esim, now=now) is not None

    @staticmethod
    def _idempotency_key(
        policy: EsimAutoTopupPolicy, esim: Esim, *, reason: str
    ) -> str:
        if reason == EsimAutoTopupPolicy.LEGACY_TRIGGER_EXPIRY:
            stamp = (
                esim.usage_expired_at.isoformat()
                if esim.usage_expired_at is not None
                else "unknown"
            )
            return f"auto-topup:{policy.pk}:{reason}:{stamp}"
        stamp = (
            esim.usage_synced_at.isoformat()
            if esim.usage_synced_at is not None
            else "unknown"
        )
        return f"auto-topup:{policy.pk}:{reason}:{stamp}"

    def _on_success(
        self, policy_id, topup, idempotency_key: str, *, fire_reason: str
    ) -> str:
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
            count_exhausted = False
            if policy.renew_mode == EsimAutoTopupPolicy.RenewMode.FIXED_COUNT:
                current = policy.remaining_count or 0
                remaining_after = max(current - 1, 0)
                policy.remaining_count = remaining_after
                update_fields.append("remaining_count")
                if remaining_after == 0:
                    policy.status = EsimAutoTopupPolicy.Status.PAUSED
                    policy.reason = EsimAutoTopupPolicy.Reason.COUNT_EXHAUSTED
                    update_fields.extend(["status", "reason"])
                    count_exhausted = True

            policy.save(update_fields=update_fields)
            succeeded = AutoTopupSucceeded(
                policy_id=str(policy.pk),
                topup_id=str(topup.pk),
                package_id=policy.package_id,
                amount=topup.amount,
                remaining_count=remaining_after,
                account_id=str(policy.account_id),
                esim_id=str(policy.esim_id),
                created_at=now,
            )

        # Events/metrics must not undo a committed purchase or policy update.
        self._safe_publish(succeeded)
        self._metric_incr("auto_topup_success_total")
        self._metric_incr("auto_topup_trigger_reason_total", reason=fire_reason)
        if count_exhausted:
            self._metric_incr("auto_topup_paused_total", reason="count_exhausted")
        return "success"

    def _pause_funds(self, policy_id, exc: InsufficientFundsError) -> str:
        with transaction.atomic():
            policy = EsimAutoTopupPolicy.objects.select_for_update().get(pk=policy_id)
            policy.status = EsimAutoTopupPolicy.Status.PAUSED
            policy.reason = EsimAutoTopupPolicy.Reason.INSUFFICIENT_FUNDS
            policy.save(update_fields=["status", "reason", "updated_at"])
            paused = AutoTopupPausedFunds(
                policy_id=str(policy.pk),
                account_id=str(policy.account_id),
                esim_id=str(policy.esim_id),
                package_id=policy.package_id,
                amount_required=exc.amount_required,
                balance=exc.account_balance,
                deposit_url=self._deposit_url(),
                created_at=timezone.now(),
            )
        self._safe_publish(paused)
        self._metric_incr("auto_topup_paused_total", reason="insufficient_funds")
        return "paused"

    def _block_package(self, policy_id) -> str:
        with transaction.atomic():
            policy = EsimAutoTopupPolicy.objects.select_for_update().get(pk=policy_id)
            policy.status = EsimAutoTopupPolicy.Status.BLOCKED
            policy.reason = EsimAutoTopupPolicy.Reason.PACKAGE_UNAVAILABLE
            policy.save(update_fields=["status", "reason", "updated_at"])
        self._metric_incr("auto_topup_paused_total", reason="package_unavailable")
        return "blocked"

    @staticmethod
    def _safe_publish(event) -> None:
        try:
            event_bus.publish(event)
        except Exception:
            logger.exception(
                "Auto top-up event publish failed (best-effort): %s",
                type(event).__name__,
            )

    @staticmethod
    def _metric_incr(name: str, **tags: str) -> None:
        try:
            metrics.incr(name, **tags)
        except Exception:
            logger.exception("Auto top-up metric incr failed (best-effort): %s", name)

    @staticmethod
    def _metric_observe(name: str, value: float, **tags: str) -> None:
        try:
            metrics.observe(name, value, **tags)
        except Exception:
            logger.exception(
                "Auto top-up metric observe failed (best-effort): %s", name
            )

    def _rollout_allows(self, policy: EsimAutoTopupPolicy) -> bool:
        return self.rollout_allows_account(policy.account)

    def rollout_allows_account(self, account) -> bool:
        """Whether ``account`` may use auto top-up under current flags/rollout."""
        if not self._master_enabled():
            return False
        mode = str(settings.AUTO_TOPUP_ROLLOUT_MODE).lower()
        if mode in ("", "off"):
            return False
        if mode == "all":
            return True
        user = account.user
        if mode == "staff":
            return bool(user.is_staff)
        if mode == "allowlist":
            allow = {str(x) for x in settings.AUTO_TOPUP_ALLOWLIST_ACCOUNT_IDS}
            return str(account.pk) in allow
        if mode == "percent":
            percent = int(settings.AUTO_TOPUP_ROLLOUT_PERCENT)
            if percent <= 0:
                return False
            if percent >= 100:
                return True
            digest = hashlib.sha256(str(account.pk).encode()).hexdigest()
            bucket = int(digest[:8], 16) % 100
            return bucket < percent
        return False

    def upsert_policy(
        self,
        *,
        esim: Esim,
        account,
        package_id: str,
        expiry_enabled: bool,
        usage_mode: str,
        renew_mode: str,
        threshold_mb: int | None,
        remaining_count: int | None,
        enabled: bool = True,
        expected_version: int | None,
        actor: str = "user",
    ) -> EsimAutoTopupPolicy:
        """Create or update policy with package validation and optimistic locking."""
        from apps.esims.exceptions import TopupPackageNotFoundError

        if not self.rollout_allows_account(account):
            raise PermissionError("Auto top-up is not enabled for this account")

        packages = self._topups.list_topups(esim)
        package = next((p for p in packages if p.external_id == package_id), None)
        if package is None:
            raise TopupPackageNotFoundError(
                f"Top-up package {package_id!r} is not available for this eSIM"
            )
        if usage_mode in (
            EsimAutoTopupPolicy.UsageMode.ZERO,
            EsimAutoTopupPolicy.UsageMode.THRESHOLD,
        ) and (package.is_unlimited or esim.usage_is_unlimited):
            raise ValueError(
                "usage_mode zero/threshold cannot be used with unlimited packages"
            )
        if (
            usage_mode == EsimAutoTopupPolicy.UsageMode.THRESHOLD
            and threshold_mb is None
        ):
            raise ValueError("threshold_mb is required when usage_mode is threshold")
        if usage_mode != EsimAutoTopupPolicy.UsageMode.THRESHOLD:
            threshold_mb = None
        if (
            enabled
            and not expiry_enabled
            and usage_mode == EsimAutoTopupPolicy.UsageMode.DISABLED
        ):
            raise ValueError(
                "enabled policy requires expiry_enabled and/or a usage_mode"
            )
        if (
            renew_mode == EsimAutoTopupPolicy.RenewMode.FIXED_COUNT
            and remaining_count is None
        ):
            raise ValueError("remaining_count is required for fixed_count")

        if Topup.objects.filter(esim=esim, status=Topup.Status.FULFILLING).exists():
            raise SpendInProgressError("Auto top-up purchase is still in progress")

        with transaction.atomic():
            existing = (
                EsimAutoTopupPolicy.objects.select_for_update()
                .filter(esim=esim)
                .first()
            )
            if existing is None:
                if expected_version is not None:
                    raise LookupError("version_conflict")
                policy = EsimAutoTopupPolicy(
                    account=account,
                    esim=esim,
                    package_id=package_id,
                    expiry_enabled=expiry_enabled,
                    usage_mode=usage_mode,
                    threshold_mb=threshold_mb,
                    renew_mode=renew_mode,
                    remaining_count=remaining_count,
                    enabled=enabled,
                    status=EsimAutoTopupPolicy.Status.ACTIVE,
                    reason="",
                    version=0,
                )
                policy.save()
                created = True
                config_changed = False
                before_config = None
            else:
                if expected_version is None or existing.version != expected_version:
                    raise LookupError("version_conflict")
                before_config = (
                    existing.expiry_enabled,
                    existing.usage_mode,
                    existing.threshold_mb,
                )
                existing.package_id = package_id
                existing.expiry_enabled = expiry_enabled
                existing.usage_mode = usage_mode
                existing.threshold_mb = threshold_mb
                after_config = (
                    existing.expiry_enabled,
                    existing.usage_mode,
                    existing.threshold_mb,
                )
                config_changed = before_config != after_config
                if config_changed:
                    existing.cooldown_until = None
                existing.renew_mode = renew_mode
                existing.remaining_count = remaining_count
                existing.enabled = enabled
                if enabled:
                    existing.status = EsimAutoTopupPolicy.Status.ACTIVE
                    existing.reason = ""
                else:
                    existing.status = EsimAutoTopupPolicy.Status.DISABLED
                    existing.reason = EsimAutoTopupPolicy.Reason.MANUAL_PAUSE
                existing.version = expected_version + 1
                existing.save()
                policy = existing
                created = False

        if created:
            self.publish_policy_created(policy, actor=actor)
        else:
            self.publish_policy_updated(policy, actor=actor)
            if config_changed and before_config is not None:
                self._safe_publish(
                    AutoTopupConfigurationChanged(
                        policy_id=str(policy.pk),
                        account_id=str(policy.account_id),
                        esim_id=str(policy.esim_id),
                        before_expiry_enabled=before_config[0],
                        before_usage_mode=before_config[1],
                        before_threshold_mb=before_config[2],
                        after_expiry_enabled=policy.expiry_enabled,
                        after_usage_mode=policy.usage_mode,
                        after_threshold_mb=policy.threshold_mb,
                        version=policy.version,
                        actor=actor,
                        created_at=timezone.now(),
                    )
                )
        return policy

    def delete_policy(
        self,
        *,
        esim: Esim,
        expected_version: int | None,
        actor: str = "user",
    ) -> None:
        """Delete policy with optimistic locking."""
        with transaction.atomic():
            existing = (
                EsimAutoTopupPolicy.objects.select_for_update()
                .filter(esim=esim)
                .first()
            )
            if existing is None:
                raise EsimAutoTopupPolicy.DoesNotExist
            if expected_version is None or existing.version != expected_version:
                raise LookupError("version_conflict")
            # Snapshot for audit before delete.
            snapshot = existing
            existing.delete()
        self.publish_policy_updated(snapshot, actor=actor)

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
            trigger_mode=policy.legacy_trigger_mode(),
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
            trigger_mode=policy.legacy_trigger_mode(),
            renew_mode=policy.renew_mode,
            version=policy.version,
            actor=actor,
            created_at=timezone.now(),
        )
