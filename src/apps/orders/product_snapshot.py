"""Immutable product snapshot helpers for Order (purchase-time copy)."""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ObjectDoesNotExist

from apps.billing.constants import LEDGER_CURRENCY

logger = logging.getLogger(__name__)


def product_snapshot_kwargs(package: Any) -> dict[str, Any]:
    """Build Order snapshot field values from a live catalog Package.

    Raises AttributeError (or similar) if required package attrs are missing;
    callers that backfill must catch and leave the row partial/empty.
    """
    location_title = ""
    country_code = getattr(package, "country_code", None) or ""
    coverage_type = ""
    location = getattr(package, "location", None)
    if location is not None:
        location_title = getattr(location, "title", None) or ""
        if not country_code:
            country_code = getattr(location, "country_code", None) or ""
        coverage_type = getattr(location, "coverage_type", None) or ""

    return {
        "package_external_id": getattr(package, "external_id", None) or "",
        "package_title": package.title or "",
        "operator_title": package.operator_title or "",
        "location_title": location_title,
        "country_code": country_code,
        "coverage_type": coverage_type,
        "data_allowance": package.data_allowance or "",
        "validity_days": package.validity_days,
        "retail_price_usd": package.price_usd,
        "currency": LEDGER_CURRENCY,
        "net_price_usd": getattr(package, "net_price_usd", None),
    }


def backfill_order_product_snapshots(Order: Any) -> int:
    """Fill empty product snapshots from each order's package.

    Idempotent: skips rows that already have ``retail_price_usd`` set.
    Never raises for missing/inconsistent packages — logs a warning and
    continues so production deploys stay safe.

    Returns the number of orders updated.
    """
    updated = 0
    qs = (
        Order.objects.filter(retail_price_usd__isnull=True)
        .select_related("package", "package__location")
        .order_by("pk")
    )
    for order in qs.iterator():
        try:
            package = order.package
        except ObjectDoesNotExist:
            logger.warning(
                "order product snapshot backfill: package missing for order_id=%s",
                order.pk,
            )
            continue
        except Exception:
            logger.warning(
                "order product snapshot backfill: cannot load package for "
                "order_id=%s",
                order.pk,
                exc_info=True,
            )
            continue

        if package is None:
            logger.warning(
                "order product snapshot backfill: package is null for order_id=%s",
                order.pk,
            )
            continue

        try:
            fields = product_snapshot_kwargs(package)
        except Exception:
            logger.warning(
                "order product snapshot backfill: inconsistent package for "
                "order_id=%s package_id=%s",
                order.pk,
                getattr(package, "pk", None),
                exc_info=True,
            )
            continue

        concrete = {f.name for f in order._meta.concrete_fields}
        fields = {k: v for k, v in fields.items() if k in concrete}
        if not fields:
            continue

        for name, value in fields.items():
            setattr(order, name, value)
        order.save(update_fields=[*fields.keys(), "updated_at"])
        updated += 1
    return updated
