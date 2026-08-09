"""Immutable product snapshot helpers for Order (purchase-time copy)."""

from __future__ import annotations

import logging
import re
from typing import Any

from django.core.exceptions import ObjectDoesNotExist

from apps.billing.constants import LEDGER_CURRENCY

logger = logging.getLogger(__name__)

_COUNTRY_CODE_RE = re.compile(r"^[A-Z]{2}$")


def normalize_coverage_snapshot(raw: Any) -> list[dict[str, Any]]:
    """Map provider/catalog coverages to the stable device coverage shape.

    Rules (locked):
    - ``country_code``: trim + uppercase; keep only exactly 2 A–Z letters
    - merge duplicate codes; ``country_name`` null when missing (do not drop)
    - operators from ``networks[].name``; case-safe dedupe; alphabetical sort
    - countries sorted by ``country_code``; empty operators allowed
    - never emit provider fields (``networks``, ``types``, …)
    """
    if not isinstance(raw, (list, tuple)):
        return []

    by_code: dict[str, dict[str, Any]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        code_raw = entry.get("code")
        if code_raw is None:
            code_raw = entry.get("country_code")
        code = str(code_raw or "").strip().upper()
        if not _COUNTRY_CODE_RE.fullmatch(code):
            continue

        name_raw = entry.get("name")
        if name_raw is None:
            name_raw = entry.get("country_name")
        if name_raw is None:
            country_name: str | None = None
        else:
            trimmed = str(name_raw).strip()
            country_name = trimmed or None

        operators = _normalize_operators(entry.get("networks"))
        if code in by_code:
            existing = by_code[code]
            if existing["country_name"] is None and country_name is not None:
                existing["country_name"] = country_name
            existing["operators"] = _merge_operators(
                existing["operators"],
                operators,
            )
        else:
            by_code[code] = {
                "country_code": code,
                "country_name": country_name,
                "operators": operators,
            }

    return [by_code[code] for code in sorted(by_code.keys())]


def _normalize_operators(networks: Any) -> list[str]:
    if not isinstance(networks, (list, tuple)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for net in networks:
        if isinstance(net, dict):
            name = str(net.get("name") or "").strip()
        elif isinstance(net, str):
            name = net.strip()
        else:
            continue
        if not name:
            continue
        fold = name.casefold()
        if fold in seen:
            continue
        seen.add(fold)
        out.append(name)
    out.sort(key=str.casefold)
    return out


def _merge_operators(left: list[str], right: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for name in [*left, *right]:
        fold = name.casefold()
        if fold in seen:
            continue
        seen.add(fold)
        merged.append(name)
    merged.sort(key=str.casefold)
    return merged


def product_snapshot_kwargs(package: Any) -> dict[str, Any]:
    """Build Order snapshot field values from a live catalog Package.

    Raises AttributeError (or similar) if required package attrs are missing;
    callers that backfill must catch and leave the row partial/empty.

    ``coverage_snapshot`` is set for new purchases only. Legacy backfill must
    not apply it from today's catalog (see ``backfill_order_product_snapshots``).
    """
    location_title = ""
    country_code = getattr(package, "country_code", None) or ""
    coverage_type = ""
    coverage_snapshot: list[dict[str, Any]] = []
    location = getattr(package, "location", None)
    if location is not None:
        location_title = getattr(location, "title", None) or ""
        if not country_code:
            country_code = getattr(location, "country_code", None) or ""
        coverage_type = getattr(location, "coverage_type", None) or ""
        coverage_snapshot = normalize_coverage_snapshot(
            getattr(location, "coverages", None)
        )

    return {
        "package_external_id": getattr(package, "external_id", None) or "",
        "package_title": package.title or "",
        "operator_title": package.operator_title or "",
        "location_title": location_title,
        "country_code": country_code,
        "coverage_type": coverage_type,
        "coverage_snapshot": coverage_snapshot,
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
        # Never reconstruct coverage from today's catalog for legacy rows.
        fields.pop("coverage_snapshot", None)
        if not fields:
            continue

        for name, value in fields.items():
            setattr(order, name, value)
        order.save(update_fields=[*fields.keys(), "updated_at"])
        updated += 1
    return updated
