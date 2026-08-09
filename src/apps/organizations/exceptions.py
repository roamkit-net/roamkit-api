"""Organization / membership domain errors (ADR 020)."""


class OrganizationError(Exception):
    """Base organization domain error."""


class MembershipInvariantError(OrganizationError):
    """Membership mutation would violate ADR 020 invariants."""


class LastOwnerError(MembershipInvariantError):
    """Cannot remove or demote the sole active owner."""


class NotAllowedError(OrganizationError):
    """Actor lacks permission for the requested membership action."""


class InviteError(OrganizationError):
    """Base invite domain error."""


class InviteConflictError(InviteError):
    """Invite conflicts with an existing membership or active invite rule."""


class InviteInvalidError(InviteError):
    """Invite token is invalid, expired, revoked, or email mismatch."""


class DeviceBindingError(OrganizationError):
    """Base device binding domain error."""


class DeviceBindingConflictError(DeviceBindingError):
    """Active binding cardinality or replace conflict."""


class DeviceBindingNotFoundError(DeviceBindingError):
    """Binding or eligible eSIM not found in organization scope."""
