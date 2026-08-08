"""Organization / membership domain errors (ADR 020)."""


class OrganizationError(Exception):
    """Base organization domain error."""


class MembershipInvariantError(OrganizationError):
    """Membership mutation would violate ADR 020 invariants."""


class LastOwnerError(MembershipInvariantError):
    """Cannot remove or demote the sole active owner."""


class NotAllowedError(OrganizationError):
    """Actor lacks permission for the requested membership action."""
