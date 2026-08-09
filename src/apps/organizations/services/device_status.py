"""Read-only device status snapshot (ADR 020 / ADR 021 Option C′).

Shared builder for org-authenticated (PR17), device-credential (PR18), and
fleet+serial (Option C′) paths. No provider refresh; no binding/inventory
mutation except refreshable ``uem_device_guid`` cache on unique serial match.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import NotFound

from apps.esims.models import Esim, EsimAutoTopupPolicy
from apps.integrations.blackberry_uem.client import (
    BlackberryUemClient,
    BlackberryUemClientError,
)
from apps.organizations.exceptions import (
    BindingNotFoundError,
    FleetCredentialInvalidError,
    IccidNotFoundError,
    UemInventoryUnavailableError,
    UemSerialMatchError,
)
from apps.organizations.models import DeviceBinding, DeviceBindingStatus
from apps.organizations.services.authz import require_view
from apps.organizations.services.context import resolve_organization_context
from apps.organizations.services.fleet_credential import verify_fleet_credential
from apps.organizations.services.uem_iccid import resolve_top_level_iccid
from apps.organizations.services.uem_serial import refresh_binding_uem_guid_from_serial

if TYPE_CHECKING:
    from apps.accounts.models import User

logger = logging.getLogger(__name__)


def _format_mb(value: int | None) -> str | None:
    if value is None:
        return None
    return f"{value} MB"


def _usage_snapshot(esim: Esim) -> dict[str, Any]:
    """Build usage from cached Esim fields only (no provider call)."""
    remaining = esim.usage_remaining_mb
    total = esim.usage_total_mb
    used: int | None = None
    if remaining is not None and total is not None and total >= remaining:
        used = total - remaining

    if esim.usage_is_unlimited is True:
        data_remaining = "unlimited"
    else:
        data_remaining = _format_mb(remaining)

    expires_at = esim.usage_expired_at
    # Unknown cache: keep object shape, null fields (do not fail the request).
    return {
        "data_remaining": data_remaining,
        "data_used": _format_mb(used),
        "expires_at": expires_at,
    }


def _auto_topup_snapshot(esim: Esim) -> dict[str, bool]:
    policy = (
        EsimAutoTopupPolicy.objects.filter(esim=esim).only("enabled", "status").first()
    )
    if policy is None:
        return {"enabled": False}
    enabled = bool(
        policy.enabled and policy.status == EsimAutoTopupPolicy.Status.ACTIVE
    )
    return {"enabled": enabled}


def _order_coverage_type(order: Any) -> str:
    coverage_raw = (getattr(order, "coverage_type", None) or "").strip().lower()
    return coverage_raw if coverage_raw in {"local", "regional", "global"} else ""


def _coverage_summary(order: Any) -> dict[str, Any] | None:
    """Light summary from Order.coverage_snapshot only (never live catalog).

    ``country_count`` is the length of the normalized snapshot list.
    """
    if not hasattr(order, "coverage_snapshot"):
        return None
    raw = order.coverage_snapshot
    # SQL NULL / missing → legacy; distinguish from snapshotted [].
    if raw is None:
        return None
    if not isinstance(raw, list):
        return {"available": False, "country_count": 0}
    country_count = len(raw)
    coverage_type = _order_coverage_type(order)
    available = coverage_type in {"regional", "global"} and country_count > 0
    return {"available": available, "country_count": country_count}


def _plan_snapshot(esim: Esim) -> dict[str, Any] | None:
    """Read-only plan metadata from Order purchase snapshot only.

    Never reads live ``order.package`` / ``location`` — avoids mixing
    immutable snapshot fields with current catalog. ``coverage_type`` is
    null for legacy orders that predate the snapshot column.
    """
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


@dataclass(frozen=True, slots=True)
class DeviceStatusSnapshot:
    device_external_id: str
    binding_status: str
    esim: dict[str, Any]
    usage: dict[str, Any]
    auto_topup: dict[str, bool]
    plan: dict[str, Any] | None
    checked_at: datetime

    def as_response_dict(self) -> dict[str, Any]:
        return {
            "device_external_id": self.device_external_id,
            "binding_status": self.binding_status,
            "esim": self.esim,
            "usage": self.usage,
            "auto_topup": self.auto_topup,
            "plan": self.plan,
            "checked_at": self.checked_at,
        }


def build_device_status_snapshot(
    binding: DeviceBinding,
    *,
    esim: Esim | None = None,
) -> DeviceStatusSnapshot:
    """Pure status snapshot from an already-authorized active binding."""
    resolved = esim if esim is not None else binding.esim
    return DeviceStatusSnapshot(
        device_external_id=binding.device_external_id,
        binding_status=binding.status,
        esim={
            "id": resolved.pk,
            "iccid": resolved.iccid,
            "status": resolved.status,
        },
        usage=_usage_snapshot(resolved),
        auto_topup=_auto_topup_snapshot(resolved),
        plan=_plan_snapshot(resolved),
        checked_at=timezone.now(),
    )


def get_device_status(
    actor: User,
    organization_id,
    *,
    device_external_id: str,
) -> DeviceStatusSnapshot:
    """Return status for an active org-scoped DeviceBinding.

    ``device_external_id`` is a lookup key only — never sufficient authz.
    Org JWT path keeps classic ``binding.esim`` resolution (no UEM in this PR).
    """
    ctx = resolve_organization_context(actor, organization_id)
    require_view(ctx)
    org = ctx.organization

    binding = (
        DeviceBinding.objects.select_related("esim", "esim__account", "esim__order")
        .filter(
            organization=org,
            device_external_id=device_external_id,
            status=DeviceBindingStatus.ACTIVE,
        )
        .first()
    )
    if binding is None:
        raise NotFound(detail="Not found.")

    esim = binding.esim
    if esim.account_id != org.account_id:
        # Defense in depth — team inventory invariant.
        raise NotFound(detail="Not found.")

    return build_device_status_snapshot(binding)


def _hash_credential(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _resolve_esim_via_uem(binding: DeviceBinding) -> Esim:
    """Read-only UEM ICCID → team Account Esim (ADR 021 staging proof).

    Side-effect free: never creates/transfers/unbinds inventory.
    """
    guid = (binding.uem_device_guid or "").strip()
    if not guid:
        raise UemInventoryUnavailableError("UEM device guid is not configured")

    if not getattr(settings, "BLACKBERRY_UEM_ENABLED", False):
        raise UemInventoryUnavailableError("UEM integration is not enabled")

    try:
        device = BlackberryUemClient().get_device_by_guid(guid)
    except BlackberryUemClientError as exc:
        logger.warning(
            "UEM device read failed for binding=%s guid=%s: %s",
            binding.pk,
            guid,
            exc,
        )
        raise UemInventoryUnavailableError(
            "UEM telephony inventory unavailable"
        ) from exc

    if device is None:
        raise UemInventoryUnavailableError("UEM device not found for mapped guid")

    iccid = resolve_top_level_iccid(device)
    account_id = binding.organization.account_id
    esim = (
        Esim.objects.select_related("order")
        .filter(account_id=account_id, iccid=iccid)
        .first()
    )
    if esim is None:
        raise IccidNotFoundError("No RoamKit data for this ICCID.")
    return esim


def _resolve_binding_and_esim_by_credential(
    *,
    device_external_id: str,
    credential: str,
) -> tuple[DeviceBinding, Esim]:
    """Shared credential + ownership resolution for device status/coverage.

    ``device_external_id`` alone is never enough. Auth failures raise 404
    without distinguishing missing binding vs bad credential.
    """
    device_external_id = (device_external_id or "").strip()
    credential = credential or ""
    if not device_external_id or not credential:
        raise NotFound(detail="Not found.")

    digest = _hash_credential(credential)
    binding = (
        DeviceBinding.objects.select_related(
            "esim", "esim__account", "esim__order", "organization"
        )
        .filter(
            device_external_id=device_external_id,
            status=DeviceBindingStatus.ACTIVE,
        )
        .first()
    )
    if binding is None or not binding.credential_hash:
        raise NotFound(detail="Not found.")
    if not hmac.compare_digest(binding.credential_hash, digest):
        raise NotFound(detail="Not found.")
    if binding.esim.account_id != binding.organization.account_id:
        raise NotFound(detail="Not found.")

    if (binding.uem_device_guid or "").strip():
        return binding, _resolve_esim_via_uem(binding)
    return binding, binding.esim


def get_device_status_by_credential(
    *,
    device_external_id: str,
    credential: str,
) -> DeviceStatusSnapshot:
    """Device-facing status via opaque credential (PR18).

    When ``DeviceBinding.uem_device_guid`` is set, resolve status Esim via
    read-only UEM ICCID lookup (staging proof). Otherwise use ``binding.esim``.
    """
    binding, esim = _resolve_binding_and_esim_by_credential(
        device_external_id=device_external_id,
        credential=credential,
    )
    return build_device_status_snapshot(binding, esim=esim)


def _esim_for_team_iccid(*, organization, iccid: str) -> Esim:
    esim = (
        Esim.objects.select_related("order")
        .filter(account_id=organization.account_id, iccid=iccid)
        .first()
    )
    if esim is None:
        raise IccidNotFoundError("No RoamKit data for this ICCID.")
    if esim.account_id != organization.account_id:
        raise IccidNotFoundError("No RoamKit data for this ICCID.")
    return esim


def get_device_status_by_fleet(
    *,
    fleet_external_id: str,
    fleet_credential: str,
    device_serial: str,
) -> DeviceStatusSnapshot:
    """Device-facing status via fleet credential + serial (ADR 021 Option C′).

    Auth boundary: valid fleet credential AND active DeviceBinding(org, serial)
    AND Esim on the organization team Account. Serial alone is never enough.
    """
    try:
        fleet = verify_fleet_credential(fleet_external_id, fleet_credential)
    except FleetCredentialInvalidError as exc:
        raise BindingNotFoundError("Device binding not found.") from exc

    serial = (device_serial or "").strip()
    if not serial:
        raise BindingNotFoundError("Device binding not found.")

    binding = (
        DeviceBinding.objects.select_related(
            "esim", "esim__account", "esim__order", "organization"
        )
        .filter(
            organization=fleet.organization,
            uem_serial_number=serial,
            status=DeviceBindingStatus.ACTIVE,
        )
        .first()
    )
    if binding is None:
        raise BindingNotFoundError("Device binding not found.")

    try:
        device = refresh_binding_uem_guid_from_serial(binding)
    except UemSerialMatchError as exc:
        logger.warning(
            "UEM serial resolve failed for binding=%s serial=%s: %s",
            binding.pk,
            serial,
            exc,
        )
        raise UemInventoryUnavailableError(
            "UEM telephony inventory unavailable"
        ) from exc

    iccid = resolve_top_level_iccid(device)
    esim = _esim_for_team_iccid(organization=binding.organization, iccid=iccid)
    return build_device_status_snapshot(binding, esim=esim)


@dataclass(frozen=True, slots=True)
class DeviceCoverageSnapshot:
    device_external_id: str
    coverage_type: str | None
    coverage: list[dict[str, Any]] | None
    checked_at: datetime

    def as_response_dict(self) -> dict[str, Any]:
        return {
            "device_external_id": self.device_external_id,
            "coverage_type": self.coverage_type,
            "coverage": self.coverage,
            "checked_at": self.checked_at,
        }


def build_device_coverage_snapshot(
    binding: DeviceBinding,
    *,
    esim: Esim | None = None,
) -> DeviceCoverageSnapshot:
    """Coverage list from Order.coverage_snapshot only (never live catalog)."""
    resolved = esim if esim is not None else binding.esim
    order = getattr(resolved, "order", None)
    coverage_type: str | None = None
    coverage: list[dict[str, Any]] | None = None
    if order is not None:
        ctype = _order_coverage_type(order)
        coverage_type = ctype or None
        raw = getattr(order, "coverage_snapshot", None)
        if raw is None:
            coverage = None
        elif isinstance(raw, list):
            # Persist shape is already normalized; strip any unexpected keys.
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
    return DeviceCoverageSnapshot(
        device_external_id=binding.device_external_id,
        coverage_type=coverage_type,
        coverage=coverage,
        checked_at=timezone.now(),
    )


def get_device_coverage_by_credential(
    *,
    device_external_id: str,
    credential: str,
) -> DeviceCoverageSnapshot:
    """Device-facing coverage via the same credential boundary as status."""
    binding, esim = _resolve_binding_and_esim_by_credential(
        device_external_id=device_external_id,
        credential=credential,
    )
    return build_device_coverage_snapshot(binding, esim=esim)
