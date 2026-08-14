"""Public Matching ID status snapshot (ADR 022).

Cache-only. Never calls the provider or live usage/package-history services.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.utils import timezone

from apps.esims.models import Esim, EsimAutoTopupPolicy

MATCHING_ID_MAX_LEN = 128
FORBIDDEN_REQUEST_KEYS = frozenset(
    {
        "device_serial",
        "device_external_id",
        "credential",
        "iccid",
        "esim_id",
        "lpa",
        "qrcode",
        "qrcode_url",
        "organization_id",
        "account_id",
        "fleet_external_id",
        "fleet_credential",
    }
)


class MatchingIdNotFoundError(Exception):
    """Capability miss — empty, too long, unknown, or not uniquely found."""


class InvalidPublicStatusRequestError(Exception):
    """Technically invalid HTTP body (not a Matching ID miss)."""


def normalize_matching_id(value: str) -> str:
    return (value or "").strip()


def redact_matching_id(value: str) -> str:
    """Log-safe form. Never return the full token."""
    token = normalize_matching_id(value)
    if len(token) < 4:
        return "••••"
    return f"{token[:2]}••••{token[-2:]}"


def mask_iccid(iccid: str) -> str:
    raw = (iccid or "").strip()
    if len(raw) < 10:
        return "••••"
    return f"{raw[:6]}••••••{raw[-4:]}"


def _format_mb(value: int | None) -> str | None:
    if value is None:
        return None
    return f"{value} MB"


def _has_usage_cache(esim: Esim) -> bool:
    return esim.usage_synced_at is not None


def _usage_snapshot(esim: Esim) -> dict[str, Any] | None:
    if not _has_usage_cache(esim):
        return None
    remaining = esim.usage_remaining_mb
    total = esim.usage_total_mb
    used: int | None = None
    if remaining is not None and total is not None and total >= remaining:
        used = total - remaining
    if esim.usage_is_unlimited is True:
        data_remaining = "unlimited"
    else:
        data_remaining = _format_mb(remaining)
    return {
        "data_remaining": data_remaining,
        "data_used": _format_mb(used),
        "expires_at": esim.usage_expired_at,
        "synced_at": esim.usage_synced_at,
    }


def _auto_topup_snapshot(esim: Esim) -> dict[str, bool]:
    exists_active = EsimAutoTopupPolicy.objects.filter(
        esim=esim,
        status=EsimAutoTopupPolicy.Status.ACTIVE,
    ).exists()
    return {"enabled": exists_active}


def _order_coverage_type(order: Any) -> str:
    coverage_raw = (getattr(order, "coverage_type", None) or "").strip().lower()
    return coverage_raw if coverage_raw in {"local", "regional", "global"} else ""


def _coverage_summary(order: Any) -> dict[str, Any] | None:
    if not hasattr(order, "coverage_snapshot"):
        return None
    raw = order.coverage_snapshot
    if raw is None:
        return None
    if not isinstance(raw, list):
        return {"available": False, "country_count": 0}
    country_count = len(raw)
    coverage_type = _order_coverage_type(order)
    available = coverage_type in {"regional", "global"} and country_count > 0
    return {"available": available, "country_count": country_count}


def _plan_snapshot(esim: Esim) -> dict[str, Any] | None:
    order = getattr(esim, "order", None)
    if order is None:
        return None
    package_title = (getattr(order, "package_title", None) or "").strip()
    location_title = (getattr(order, "location_title", None) or "").strip()
    country_code = (getattr(order, "country_code", None) or "").strip().upper()
    data_allowance = (getattr(order, "data_allowance", None) or "").strip()
    validity_days = getattr(order, "validity_days", None)
    coverage_type = _order_coverage_type(order)
    title = package_title or location_title
    has_any = bool(
        title
        or data_allowance
        or validity_days is not None
        or country_code
        or coverage_type
    )
    if not has_any:
        return None
    return {
        "title": title or None,
        "data_allowance": data_allowance or None,
        "validity_days": validity_days,
        "country_code": country_code or None,
        "coverage_type": coverage_type or None,
        "location_title": location_title or None,
        "coverage_summary": _coverage_summary(order),
    }


def _coverage_snapshot(esim: Esim) -> dict[str, Any] | None:
    order = getattr(esim, "order", None)
    if order is None:
        return None
    raw = getattr(order, "coverage_snapshot", None)
    if raw is None:
        return None
    coverage_type = _order_coverage_type(order) or None
    if isinstance(raw, list):
        coverage = [
            {
                "country_code": str(item.get("country_code") or ""),
                "country_name": item.get("country_name"),
                "operators": list(item.get("operators") or []),
            }
            for item in raw
            if isinstance(item, dict) and item.get("country_code")
        ]
    else:
        coverage = []
    return {"coverage_type": coverage_type, "coverage": coverage}


def resolve_esim_by_matching_id(matching_id: str) -> Esim:
    token = normalize_matching_id(matching_id)
    if not token or len(token) > MATCHING_ID_MAX_LEN:
        raise MatchingIdNotFoundError()
    matches = list(
        Esim.objects.select_related("order").filter(
            matching_id=token,
            archived_at__isnull=True,
        )
    )
    if len(matches) != 1:
        raise MatchingIdNotFoundError()
    return matches[0]


@dataclass(frozen=True, slots=True)
class PublicEsimStatusSnapshot:
    esim: dict[str, Any]
    usage: dict[str, Any] | None
    auto_topup: dict[str, bool]
    plan: dict[str, Any] | None
    packages: dict[str, Any] | None
    coverage: dict[str, Any] | None
    checked_at: datetime

    def as_response_dict(self) -> dict[str, Any]:
        return {
            "esim": self.esim,
            "usage": self.usage,
            "auto_topup": self.auto_topup,
            "plan": self.plan,
            "packages": self.packages,
            "coverage": self.coverage,
            "checked_at": self.checked_at,
        }


def build_public_esim_status(esim: Esim) -> PublicEsimStatusSnapshot:
    """Local Order + Topup cannot reproduce Airalo package statuses.

    ``packages`` is therefore always null (ADR 022 audit). Plan/allowance
    still come from the Order purchase snapshot.
    """
    return PublicEsimStatusSnapshot(
        esim={
            "iccid": mask_iccid(esim.iccid),
            "status": esim.status,
        },
        usage=_usage_snapshot(esim),
        auto_topup=_auto_topup_snapshot(esim),
        plan=_plan_snapshot(esim),
        packages=None,
        coverage=_coverage_snapshot(esim),
        checked_at=timezone.now(),
    )


def get_public_esim_status(matching_id: str) -> PublicEsimStatusSnapshot:
    esim = resolve_esim_by_matching_id(matching_id)
    return build_public_esim_status(esim)
