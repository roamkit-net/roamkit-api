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


class UemInventoryUnavailableError(DeviceBindingError):
    """UEM telephony inventory missing/stale or unreadable (ADR 021)."""


class IccidNotFoundError(DeviceBindingError):
    """UEM ICCID present but no matching non-archived Esim in RoamKit."""


class IccidAmbiguousError(DeviceBindingError):
    """UEM ICCID matches more than one non-archived Esim (fail closed)."""


class UemSerialMatchError(DeviceBindingError):
    """UEM serialNumber resolve failed for a non-count reason (transport/config)."""


class DeviceNotFoundError(DeviceBindingError):
    """UEM serialNumber match count is 0 (ADR 021)."""


class DeviceAmbiguousError(DeviceBindingError):
    """UEM serialNumber match count is greater than 1 (ADR 021)."""


class BindingNotFoundError(DeviceBindingError):
    """Fleet/PR18 binding auth failed or no active binding for the device."""


class FleetCredentialError(OrganizationError):
    """Base fleet credential domain error (ADR 021 Option C′)."""


class FleetCredentialConflictError(FleetCredentialError):
    """Fleet credential already exists or conflicts with org invariants."""


class FleetCredentialInvalidError(FleetCredentialError):
    """Fleet external id / secret missing, wrong, or outside grace window."""
