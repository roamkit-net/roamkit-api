"""Read-only UEM device status snapshot (ADR 020).

Shared builder for org-authenticated (PR17) and device-credential (PR18)
paths. No provider refresh, no BlackBerry sync.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from django.utils import timezone
from rest_framework.exceptions import NotFound

from apps.esims.models import EsimAutoTopupPolicy
from apps.organizations.models import DeviceBinding, DeviceBindingStatus
from apps.organizations.services.authz import require_view
from apps.organizations.services.context import resolve_organization_context

if TYPE_CHECKING:
    from apps.accounts.models import User
    from apps.esims.models import Esim


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


@dataclass(frozen=True, slots=True)
class DeviceStatusSnapshot:
    device_external_id: str
    binding_status: str
    esim: dict[str, Any]
    usage: dict[str, Any]
    auto_topup: dict[str, bool]
    checked_at: datetime


def build_device_status_snapshot(binding: DeviceBinding) -> DeviceStatusSnapshot:
    """Pure status snapshot from an already-authorized active binding."""
    esim = binding.esim
    return DeviceStatusSnapshot(
        device_external_id=binding.device_external_id,
        binding_status=binding.status,
        esim={
            "id": esim.pk,
            "iccid": esim.iccid,
            "status": esim.status,
        },
        usage=_usage_snapshot(esim),
        auto_topup=_auto_topup_snapshot(esim),
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
    """
    ctx = resolve_organization_context(actor, organization_id)
    require_view(ctx)
    org = ctx.organization

    binding = (
        DeviceBinding.objects.select_related("esim", "esim__account")
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


def get_device_status_by_credential(
    *,
    device_external_id: str,
    credential: str,
) -> DeviceStatusSnapshot:
    """Device-facing status via opaque credential (PR18).

    ``device_external_id`` alone is never enough. Failures return 404 without
    distinguishing missing binding vs bad credential.
    """
    device_external_id = (device_external_id or "").strip()
    credential = credential or ""
    if not device_external_id or not credential:
        raise NotFound(detail="Not found.")

    digest = _hash_credential(credential)
    binding = (
        DeviceBinding.objects.select_related("esim", "esim__account", "organization")
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

    return build_device_status_snapshot(binding)
