"""eSIM Celery tasks (auto top-up beat)."""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="esims.refresh_airalo_usage")
def refresh_airalo_usage() -> dict[str, int]:
    """Refresh stale Airalo usage for provider-driven lifecycle statuses."""
    from datetime import timedelta

    from django.conf import settings
    from django.db.models import Q
    from django.utils import timezone

    from apps.esims.models import Esim
    from apps.esims.services.usage_service import UsageService
    from shared.providers.factory import get_topup_provider

    stats = {"eligible": 0, "refreshed": 0, "failed": 0}
    if not settings.AIRALO_ENABLED:
        logger.info("Skipping Airalo usage refresh (AIRALO_ENABLED=false)")
        return stats

    stale_before = timezone.now() - timedelta(seconds=300)
    eligible = (
        Esim.objects.filter(
            status__in=(
                Esim.Status.IN_USE,
                Esim.Status.PURCHASED,
                Esim.Status.UNKNOWN,
            )
        )
        .filter(Q(usage_synced_at__isnull=True) | Q(usage_synced_at__lt=stale_before))
        .order_by("pk")
    )
    stats["eligible"] = eligible.count()
    usage_service = UsageService(get_topup_provider())
    for esim in eligible.iterator():
        try:
            usage_service.get_usage(esim)
        except Exception:
            stats["failed"] += 1
            logger.exception(
                "Periodic Airalo usage refresh failed for esim_id=%s",
                esim.pk,
            )
        else:
            stats["refreshed"] += 1

    logger.info("Periodic Airalo usage refresh finished: %s", stats)
    return stats


@shared_task(name="esims.evaluate_auto_topups")
def evaluate_auto_topups() -> dict[str, int]:
    """Periodic beat: evaluate enabled auto top-up policies."""
    from django.conf import settings

    from apps.esims.services.auto_topup_service import AutoTopupService
    from shared.providers.factory import get_topup_provider

    if not settings.AUTO_TOPUP_ENABLED or not settings.BILLING_ENABLED:
        logger.info(
            "Skipping auto top-up evaluate "
            "(AUTO_TOPUP_ENABLED=%s BILLING_ENABLED=%s)",
            settings.AUTO_TOPUP_ENABLED,
            settings.BILLING_ENABLED,
        )
        return {"disabled": 1}

    stats = AutoTopupService(get_topup_provider()).evaluate_due()
    logger.info("Auto top-up evaluate finished: %s", stats)
    return stats


@shared_task(name="esims.evaluate_auto_topup_policy")
def evaluate_auto_topup_policy(policy_id: str) -> str:
    """One-shot evaluate after a manual insufficient-funds resume (Me PUT)."""
    from django.conf import settings

    from apps.esims.services.auto_topup_service import AutoTopupService
    from shared.providers.factory import get_topup_provider

    if not settings.AUTO_TOPUP_ENABLED or not settings.BILLING_ENABLED:
        logger.info(
            "Skipping auto top-up policy evaluate "
            "(AUTO_TOPUP_ENABLED=%s BILLING_ENABLED=%s policy_id=%s)",
            settings.AUTO_TOPUP_ENABLED,
            settings.BILLING_ENABLED,
            policy_id,
        )
        return "skipped"

    outcome = AutoTopupService(get_topup_provider()).evaluate_one(policy_id)
    logger.info("Auto top-up policy %s evaluate finished: %s", policy_id, outcome)
    return outcome
