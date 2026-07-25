"""Billing Celery tasks (subscriptions + reconcile)."""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="billing.renew_subscriptions")
def renew_subscriptions() -> dict[str, int]:
    """Daily beat: renew due ACTIVE subscriptions (gated by flags)."""
    from django.conf import settings

    from apps.billing.services.subscription import subscription_service

    if not settings.BILLING_ENABLED or not settings.SUBSCRIPTIONS_ENABLED:
        logger.info(
            "Skipping subscription renew "
            "(BILLING_ENABLED=%s SUBSCRIPTIONS_ENABLED=%s)",
            settings.BILLING_ENABLED,
            settings.SUBSCRIPTIONS_ENABLED,
        )
        return {"renewed": 0, "paused": 0, "skipped": 0, "disabled": 1}

    stats = subscription_service.renew_due()
    logger.info("Subscription renew finished: %s", stats)
    return stats


@shared_task(name="billing.reconcile_balances")
def reconcile_balances() -> dict[str, int]:
    """Daily beat: alert on Account.balance vs ledger SUM drift (no auto-fix)."""
    from apps.billing.services.reconcile import reconcile_service

    stats = reconcile_service.reconcile()
    logger.info("Balance reconcile finished: %s", stats)
    return stats
