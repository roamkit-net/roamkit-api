"""DeviceBinding service — team Account eSIM ↔ device_external_id (ADR 020)."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound

from apps.esims.models import Esim
from apps.organizations.exceptions import (
    DeviceBindingConflictError,
    DeviceBindingNotFoundError,
)
from apps.organizations.models import (
    DeviceBinding,
    DeviceBindingEvent,
    DeviceBindingEventAction,
    DeviceBindingStatus,
)
from apps.organizations.services.authz import require_device_bind, require_view
from apps.organizations.services.context import resolve_organization_context

if TYPE_CHECKING:
    from apps.accounts.models import User
    from apps.organizations.models import Organization


def _hash_credential(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_credential() -> str:
    return secrets.token_urlsafe(32)


def _team_esim_or_404(organization: Organization, esim_id) -> Esim:
    """Resolve eSIM owned by the org team Account (hide cross-account)."""
    esim = (
        Esim.objects.select_related("account")
        .filter(pk=esim_id, account_id=organization.account_id)
        .first()
    )
    if esim is None:
        raise NotFound(detail="Not found.")
    return esim


def _record_event(
    *,
    organization: Organization,
    binding: DeviceBinding,
    esim: Esim,
    action: str,
    actor: User | None,
    previous_binding: DeviceBinding | None = None,
) -> DeviceBindingEvent:
    return DeviceBindingEvent.objects.create(
        organization=organization,
        binding=binding,
        esim=esim,
        action=action,
        actor=actor,
        device_external_id=binding.device_external_id,
        previous_binding=previous_binding,
    )


def _clear_credential(binding: DeviceBinding) -> None:
    binding.credential_hash = ""
    binding.credential_issued_at = None
    binding.save(
        update_fields=["credential_hash", "credential_issued_at", "updated_at"]
    )


def _set_credential(binding: DeviceBinding, *, actor: User | None, rotate: bool) -> str:
    raw = _new_credential()
    binding.credential_hash = _hash_credential(raw)
    binding.credential_issued_at = timezone.now()
    binding.save(
        update_fields=["credential_hash", "credential_issued_at", "updated_at"]
    )
    _record_event(
        organization=binding.organization,
        binding=binding,
        esim=binding.esim,
        action=(
            DeviceBindingEventAction.CREDENTIAL_ROTATE
            if rotate
            else DeviceBindingEventAction.CREDENTIAL_ISSUE
        ),
        actor=actor,
    )
    return raw


@dataclass(frozen=True, slots=True)
class DeviceBindingCreateResult:
    binding: DeviceBinding
    credential: str


def list_device_bindings(actor: User, organization_id) -> list[DeviceBinding]:
    ctx = resolve_organization_context(actor, organization_id)
    require_view(ctx)
    return list(
        DeviceBinding.objects.filter(organization=ctx.organization)
        .select_related("esim", "bound_by")
        .order_by("-created_at")
    )


def get_device_binding(actor: User, organization_id, binding_id) -> DeviceBinding:
    ctx = resolve_organization_context(actor, organization_id)
    require_view(ctx)
    binding = (
        DeviceBinding.objects.select_related("esim", "bound_by")
        .filter(pk=binding_id, organization=ctx.organization)
        .first()
    )
    if binding is None:
        raise NotFound(detail="Not found.")
    return binding


@transaction.atomic
def create_device_binding(
    actor: User,
    organization_id,
    *,
    esim_id,
    replace: bool = False,
) -> DeviceBindingCreateResult:
    """Create an active binding and issue a one-time plaintext credential.

    Mints a new RoamKit ``device_external_id``. Never accepts client account_id
    or client-supplied device ids as authz.
    """
    ctx = resolve_organization_context(actor, organization_id)
    require_device_bind(ctx)
    org = ctx.organization
    esim = _team_esim_or_404(org, esim_id)

    existing = (
        DeviceBinding.objects.select_for_update()
        .filter(esim=esim, status=DeviceBindingStatus.ACTIVE)
        .first()
    )
    previous: DeviceBinding | None = None
    action = DeviceBindingEventAction.BIND
    if existing is not None:
        if not replace:
            raise DeviceBindingConflictError(
                "eSIM already has an active device binding"
            )
        previous = existing
        action = DeviceBindingEventAction.REBIND
        # Clear active slot before insert (partial unique on status=active).
        previous.status = DeviceBindingStatus.REPLACED
        previous.unbound_at = timezone.now()
        previous.unbound_by = actor
        previous.credential_hash = ""
        previous.credential_issued_at = None
        previous.save(
            update_fields=[
                "status",
                "unbound_at",
                "unbound_by",
                "credential_hash",
                "credential_issued_at",
                "updated_at",
            ]
        )

    device_external_id = str(uuid.uuid4())
    try:
        binding = DeviceBinding.objects.create(
            organization=org,
            esim=esim,
            device_external_id=device_external_id,
            status=DeviceBindingStatus.ACTIVE,
            bound_by=actor,
        )
    except IntegrityError as exc:
        raise DeviceBindingConflictError("Active device binding conflict") from exc

    if previous is not None:
        previous.replaced_by = binding
        previous.save(update_fields=["replaced_by", "updated_at"])

    _record_event(
        organization=org,
        binding=binding,
        esim=esim,
        action=action,
        actor=actor,
        previous_binding=previous,
    )
    # Refresh organization/esim for credential audit FK access.
    binding = DeviceBinding.objects.select_related("organization", "esim").get(
        pk=binding.pk
    )
    credential = _set_credential(binding, actor=actor, rotate=False)
    return DeviceBindingCreateResult(binding=binding, credential=credential)


@transaction.atomic
def rotate_device_credential(
    actor: User, organization_id, binding_id
) -> DeviceBindingCreateResult:
    """Issue a new credential; previous secret stops working immediately."""
    ctx = resolve_organization_context(actor, organization_id)
    require_device_bind(ctx)
    org = ctx.organization
    binding = (
        DeviceBinding.objects.select_for_update()
        .select_related("esim", "organization")
        .filter(pk=binding_id, organization=org)
        .first()
    )
    if binding is None:
        raise NotFound(detail="Not found.")
    if binding.status != DeviceBindingStatus.ACTIVE:
        raise DeviceBindingNotFoundError("Device binding is not active")
    credential = _set_credential(binding, actor=actor, rotate=True)
    return DeviceBindingCreateResult(binding=binding, credential=credential)


@transaction.atomic
def unbind_device_binding(actor: User, organization_id, binding_id) -> DeviceBinding:
    ctx = resolve_organization_context(actor, organization_id)
    require_device_bind(ctx)
    org = ctx.organization
    binding = (
        DeviceBinding.objects.select_for_update()
        .select_related("esim")
        .filter(pk=binding_id, organization=org)
        .first()
    )
    if binding is None:
        raise NotFound(detail="Not found.")
    if binding.status != DeviceBindingStatus.ACTIVE:
        raise DeviceBindingNotFoundError("Device binding is not active")

    binding.status = DeviceBindingStatus.UNBOUND
    binding.unbound_at = timezone.now()
    binding.unbound_by = actor
    binding.credential_hash = ""
    binding.credential_issued_at = None
    binding.save(
        update_fields=[
            "status",
            "unbound_at",
            "unbound_by",
            "credential_hash",
            "credential_issued_at",
            "updated_at",
        ]
    )
    _record_event(
        organization=org,
        binding=binding,
        esim=binding.esim,
        action=DeviceBindingEventAction.UNBIND,
        actor=actor,
    )
    return binding
