"""Role → permission matrix (ADR 020)."""

from __future__ import annotations

from dataclasses import dataclass

from apps.organizations.models import MembershipRole


@dataclass(frozen=True, slots=True)
class OrgPermissions:
    can_view: bool
    can_spend: bool
    can_manage_members: bool
    can_invite: bool
    can_transfer_ownership: bool
    can_archive_org: bool
    can_assign_esim: bool
    can_device_bind: bool


_MATRIX: dict[str, OrgPermissions] = {
    MembershipRole.OWNER: OrgPermissions(
        can_view=True,
        can_spend=True,
        can_manage_members=True,
        can_invite=True,
        can_transfer_ownership=True,
        can_archive_org=True,
        can_assign_esim=True,
        can_device_bind=True,
    ),
    MembershipRole.ADMIN: OrgPermissions(
        can_view=True,
        can_spend=True,
        can_manage_members=True,
        can_invite=True,
        can_transfer_ownership=False,
        can_archive_org=False,
        can_assign_esim=True,
        can_device_bind=True,
    ),
    MembershipRole.MEMBER: OrgPermissions(
        can_view=True,
        can_spend=True,
        can_manage_members=False,
        can_invite=False,
        can_transfer_ownership=False,
        can_archive_org=False,
        can_assign_esim=True,
        can_device_bind=False,
    ),
    MembershipRole.VIEWER: OrgPermissions(
        can_view=True,
        can_spend=False,
        can_manage_members=False,
        can_invite=False,
        can_transfer_ownership=False,
        can_archive_org=False,
        can_assign_esim=False,
        can_device_bind=False,
    ),
}


def permissions_for_role(role: str) -> OrgPermissions:
    try:
        return _MATRIX[role]
    except KeyError as exc:
        raise ValueError(f"Unknown membership role: {role}") from exc
