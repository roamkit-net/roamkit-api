"""DeviceBinding service — team Account eSIM ↔ device_external_id (ADR 020)."""

from __future__ import annotations

import uuid
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
) -> DeviceBinding:
    """Create an active binding; optionally replace an existing active one.

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
        previous.save(
            update_fields=["status", "unbound_at", "unbound_by", "updated_at"]
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
    return binding


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
    binding.save(update_fields=["status", "unbound_at", "unbound_by", "updated_at"])
    _record_event(
        organization=org,
        binding=binding,
        esim=binding.esim,
        action=DeviceBindingEventAction.UNBIND,
        actor=actor,
    )
    return binding
