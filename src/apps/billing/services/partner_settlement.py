"""Resolve the settlement Account for a PartnerChannel (ADR 024).

Side-effect free. Does not create Accounts, convert ``Account.kind``, lock
rows, or write the ledger. Callers that already know the channel may pass it
here. This module does not decide whether the user may access the channel.
"""

from __future__ import annotations

from apps.billing.models import Account, AccountKind
from apps.billing.partner_channel import PartnerChannel
from apps.organizations.models import Organization


class PartnerChannelOwnershipInvalid(Exception):
    """Kind and owner columns are not a legal PartnerChannel shape."""


class PartnerSettlementAccountMissing(Exception):
    """The expected settlement Account is missing or owned by someone else."""


def resolve_partner_settlement_account(channel: PartnerChannel) -> Account:
    """Return the Account that settles this channel.

    Individual channels settle on the owner's personal Account. Team channels
    settle on that organization's Account. There is no fallback between those
    relations, and a missing Account is not created here.
    """
    if channel.kind == PartnerChannel.Kind.INDIVIDUAL:
        _require_individual_owner(channel)
        return _personal_settlement_account(channel)
    if channel.kind == PartnerChannel.Kind.TEAM:
        _require_team_owner(channel)
        return _organization_settlement_account(channel)
    raise PartnerChannelOwnershipInvalid(
        "PartnerChannel kind is not individual or team"
    )


def _require_individual_owner(channel: PartnerChannel) -> None:
    if channel.owner_user_id is None or channel.organization_id is not None:
        raise PartnerChannelOwnershipInvalid(
            "Individual PartnerChannel ownership is not valid"
        )


def _require_team_owner(channel: PartnerChannel) -> None:
    if channel.organization_id is None or channel.owner_user_id is not None:
        raise PartnerChannelOwnershipInvalid(
            "Team PartnerChannel ownership is not valid"
        )


def _personal_settlement_account(channel: PartnerChannel) -> Account:
    owner = channel.owner_user
    if owner is None or owner.pk != channel.owner_user_id:
        raise PartnerChannelOwnershipInvalid(
            "Individual PartnerChannel ownership is not valid"
        )
    try:
        account = owner.billing_account
    except Account.DoesNotExist as exc:
        raise PartnerSettlementAccountMissing(
            "Individual PartnerChannel has no personal settlement Account"
        ) from exc
    if not _is_owner_personal_account(account, channel.owner_user_id):
        raise PartnerSettlementAccountMissing(
            "Individual settlement Account does not belong to the channel owner"
        )
    return account


def _organization_settlement_account(channel: PartnerChannel) -> Account:
    organization = channel.organization
    if organization is None or organization.pk != channel.organization_id:
        raise PartnerChannelOwnershipInvalid(
            "Team PartnerChannel ownership is not valid"
        )
    try:
        account = organization.account
    except Account.DoesNotExist as exc:
        raise PartnerSettlementAccountMissing(
            "Team PartnerChannel has no organization settlement Account"
        ) from exc
    if not _is_organization_account(account, organization):
        raise PartnerSettlementAccountMissing(
            "Team settlement Account does not belong to the channel organization"
        )
    return account


def _is_owner_personal_account(account: Account, owner_user_id: int) -> bool:
    """Personal kind, this user, and no Organization pointing at the Account."""
    if account.kind != AccountKind.PERSONAL:
        return False
    if account.user_id is None or account.user_id != owner_user_id:
        return False
    return not Organization.objects.filter(account_id=account.pk).exists()


def _is_organization_account(account: Account, organization: Organization) -> bool:
    """Organization kind, no user, and this Organization's Account row."""
    if account.kind != AccountKind.ORGANIZATION or account.user_id is not None:
        return False
    if organization.account_id != account.pk:
        return False
    try:
        linked = account.organization
    except Organization.DoesNotExist:
        return False
    return linked.pk == organization.pk
