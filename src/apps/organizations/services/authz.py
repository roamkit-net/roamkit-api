"""Authorization helpers for Organization actions (ADR 020)."""

from __future__ import annotations

from rest_framework.exceptions import PermissionDenied

from apps.organizations.services.context import AccountContext, require_org_mutation


def require_permission(context: AccountContext, permission: str) -> None:
    """Raise 403 if ``context.permissions`` lacks ``permission``."""
    perms = context.permissions
    if perms is None or not getattr(perms, permission, False):
        raise PermissionDenied(detail="Permission denied.")


def require_view(context: AccountContext) -> None:
    require_permission(context, "can_view")


def require_spend(context: AccountContext) -> None:
    """Spend gate for team Account context (orders; top-ups later).

    Requires active Organization + ``can_spend`` (owner/admin/member).
    Personal context has no org permissions object — callers must only invoke
    this for ``kind == "organization"``.
    """
    require_org_mutation(context)
    require_permission(context, "can_spend")


def require_manage_members(context: AccountContext) -> None:
    require_org_mutation(context)
    require_permission(context, "can_manage_members")


def require_transfer_ownership(context: AccountContext) -> None:
    require_org_mutation(context)
    require_permission(context, "can_transfer_ownership")


def require_archive_org(context: AccountContext) -> None:
    require_org_mutation(context)
    require_permission(context, "can_archive_org")


def require_invite(context: AccountContext) -> None:
    require_org_mutation(context)
    require_permission(context, "can_invite")
