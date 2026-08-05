"""Aggregated ops dashboard payload (single response for the ops home page)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Max, Q, Sum
from django.utils import timezone

from apps.billing.models import CreditLedgerEntry, DepositRequest, LedgerReferenceType
from apps.esims.models import Esim, Topup
from apps.ops.services.timeline import build_global_activity
from apps.orders.models import Order
from core.health.views import _check_database, _check_redis

User = get_user_model()

DASHBOARD_SCHEMA_VERSION = 1

ACTIVE_ESIM_STATUSES = (
    Esim.Status.INSTALLED,
    Esim.Status.ACTIVATED,
    Esim.Status.IN_USE,
)

PENDING_ORDER_STATUSES = (
    Order.Status.PENDING_PAYMENT,
    Order.Status.PAID,
    Order.Status.FULFILLING,
)

STUCK_INSTALL_STATUSES = (
    Esim.Status.PURCHASED,
    Esim.Status.INSTALLATION_STARTED,
)

SPEND_REFERENCE_TYPES = (
    LedgerReferenceType.ORDER,
    LedgerReferenceType.TOPUP,
    LedgerReferenceType.SUBSCRIPTION,
)


def _day_start(now=None):
    now = now or timezone.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _dec(value: Decimal | None) -> str:
    if value is None:
        return "0.000000"
    return f"{value:.6f}"


def _health() -> dict[str, Any]:
    db_ok, db_detail = _check_database()
    redis_ok, redis_detail = _check_redis()
    wc_enabled = bool(getattr(settings, "WALLETCONNECT_ENABLED", False))
    return {
        "api": {"status": "ok", "detail": "ok"},
        "database": {
            "status": "ok" if db_ok else "error",
            "detail": db_detail,
        },
        "redis": {
            "status": "ok" if redis_ok else "error",
            "detail": redis_detail,
        },
        "walletconnect": {
            "status": "enabled" if wc_enabled else "disabled",
            "detail": "config flag only",
        },
        "airalo": {"status": "unknown", "detail": "not probed"},
        "polygon_rpc": {"status": "unknown", "detail": "not probed"},
        "email": {"status": "unknown", "detail": "not probed"},
        "celery": {"status": "unknown", "detail": "not probed"},
    }


def build_dashboard() -> dict[str, Any]:
    """Compute all dashboard widgets in one pass (aggregations, no N+1)."""
    now = timezone.now()
    today = _day_start(now)
    day_ago = now - timedelta(days=1)
    minutes_30 = now - timedelta(minutes=30)
    days_30 = now - timedelta(days=30)

    users_total = User.objects.count()
    new_users_today = User.objects.filter(created_at__gte=today).count()

    esim_counts = Esim.objects.aggregate(
        active=Count("id", filter=Q(status__in=ACTIVE_ESIM_STATUSES)),
        new_today=Count("id", filter=Q(created_at__gte=today)),
        stuck=Count(
            "id",
            filter=Q(
                status__in=STUCK_INSTALL_STATUSES,
                updated_at__lt=day_ago,
            ),
        ),
    )

    order_agg = Order.objects.aggregate(
        pending=Count("id", filter=Q(status__in=PENDING_ORDER_STATUSES)),
        failed_24h=Count(
            "id",
            filter=Q(status=Order.Status.FAILED, updated_at__gte=day_ago),
        ),
        today_count=Count("id", filter=Q(created_at__gte=today)),
        revenue_today=Sum(
            "retail_price_usd",
            filter=Q(
                status=Order.Status.FULFILLED,
                created_at__gte=today,
            ),
        ),
    )

    deposit_agg = DepositRequest.objects.aggregate(
        pending=Count("id", filter=Q(status=DepositRequest.Status.PENDING)),
        pending_amount=Sum(
            "amount_requested",
            filter=Q(status=DepositRequest.Status.PENDING),
        ),
        completed_today=Count(
            "id",
            filter=Q(
                status=DepositRequest.Status.COMPLETED,
                created_at__gte=today,
            ),
        ),
        deposits_today_amount=Sum(
            "amount_credited",
            filter=Q(
                status=DepositRequest.Status.COMPLETED,
                created_at__gte=today,
            ),
        ),
        avg_30d=Avg(
            "amount_credited",
            filter=Q(
                status=DepositRequest.Status.COMPLETED,
                created_at__gte=days_30,
                amount_credited__isnull=False,
            ),
        ),
        max_30d=Max(
            "amount_credited",
            filter=Q(
                status=DepositRequest.Status.COMPLETED,
                created_at__gte=days_30,
                amount_credited__isnull=False,
            ),
        ),
        stale_pending=Count(
            "id",
            filter=Q(
                status=DepositRequest.Status.PENDING,
                created_at__lt=minutes_30,
            ),
        ),
    )

    topup_agg = Topup.objects.aggregate(
        pending=Count("id", filter=Q(status=Topup.Status.FULFILLING)),
        today=Count(
            "id",
            filter=Q(status=Topup.Status.FULFILLED, created_at__gte=today),
        ),
        stale_fulfilling=Count(
            "id",
            filter=Q(
                status=Topup.Status.FULFILLING,
                created_at__lt=minutes_30,
            ),
        ),
    )

    spend_today = CreditLedgerEntry.objects.filter(
        created_at__gte=today,
        reference_type__in=SPEND_REFERENCE_TYPES,
        delta__lt=0,
    ).aggregate(total=Sum("delta"))["total"]
    spend_abs = abs(spend_today) if spend_today is not None else Decimal("0")

    top_destinations = list(
        Order.objects.exclude(country_code="")
        .values("country_code")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )
    top_packages = list(
        Order.objects.filter(status=Order.Status.FULFILLED)
        .exclude(package_title="")
        .values("package_title")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )

    alerts: list[dict[str, Any]] = []
    if deposit_agg["stale_pending"]:
        alerts.append(
            {
                "code": "deposit_waiting_gt_30m",
                "severity": "error",
                "title": "Deposit waiting >30 min",
                "count": deposit_agg["stale_pending"],
            }
        )
    if order_agg["failed_24h"]:
        alerts.append(
            {
                "code": "failed_orders_24h",
                "severity": "error",
                "title": "Failed orders (24h)",
                "count": order_agg["failed_24h"],
            }
        )
    if topup_agg["stale_fulfilling"]:
        alerts.append(
            {
                "code": "topup_fulfilling_gt_30m",
                "severity": "error",
                "title": "Top-up fulfilling >30 min",
                "count": topup_agg["stale_fulfilling"],
            }
        )

    revenue = order_agg["revenue_today"] or Decimal("0")

    return {
        "schema_version": DASHBOARD_SCHEMA_VERSION,
        "kpi": {
            "users_total": users_total,
            "active_esims": esim_counts["active"] or 0,
            "new_users_today": new_users_today,
            "orders_today": order_agg["today_count"] or 0,
            "deposits_today": deposit_agg["completed_today"] or 0,
            "revenue_today": f"{revenue:.2f}",
            "new_esims_today": esim_counts["new_today"] or 0,
            "topups_today": topup_agg["today"] or 0,
        },
        "pending_work": {
            "pending_deposits": deposit_agg["pending"] or 0,
            "pending_topups": topup_agg["pending"] or 0,
            "pending_orders": order_agg["pending"] or 0,
            "failed_orders_24h": order_agg["failed_24h"] or 0,
            "stuck_installs": esim_counts["stuck"] or 0,
        },
        "financial": {
            "deposits_today_amount": _dec(deposit_agg["deposits_today_amount"]),
            "spend_today_amount": _dec(spend_abs),
            "average_deposit_30d": _dec(deposit_agg["avg_30d"]),
            "largest_deposit_30d": _dec(deposit_agg["max_30d"]),
            "pending_deposit_amount": _dec(deposit_agg["pending_amount"]),
            "revenue_today": f"{revenue:.2f}",
        },
        "top_destinations": [
            {"country_code": row["country_code"], "count": row["count"]}
            for row in top_destinations
        ],
        "top_packages": [
            {"package_title": row["package_title"], "count": row["count"]}
            for row in top_packages
        ],
        "alerts": alerts,
        "health": _health(),
        "activity": build_global_activity(limit=50),
    }
