"""eSIM Celery tasks (auto top-up beat)."""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


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
