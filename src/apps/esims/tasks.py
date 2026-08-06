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
