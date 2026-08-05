"""Member list/detail helpers for ops API."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.contrib.auth import get_user_model
from django.db.models import Prefetch, Sum

from apps.billing.models import DepositRequest
from apps.esims.models import Esim, EsimLifecycleEvent
from apps.ops.services.timeline import build_user_timeline
from apps.wallet.models import WalletAddress

User = get_user_model()

USER_DETAIL_SCHEMA_VERSION = 1

ESIM_STATUS_LABELS = {
    Esim.Status.PURCHASED: "Waiting install",
    Esim.Status.INSTALLATION_STARTED: "Waiting install",
    Esim.Status.INSTALLED: "Installed",
    Esim.Status.ACTIVATED: "Activated",
    Esim.Status.IN_USE: "In use",
    Esim.Status.EXHAUSTED: "Consumed",
    Esim.Status.EXPIRED: "Expired",
    Esim.Status.UNKNOWN: "Unknown",
}


def user_badges(user: User) -> list[str]:
    badges: list[str] = []
    if getattr(user, "google_sub", None):
        badges.append("google")
    has_wallet = bool(getattr(user, "wallet_address", None))
    account = getattr(user, "billing_account", None)
    if not has_wallet and account is not None:
        identity = getattr(account, "wallet_identity", None)
        if identity is not None:
            has_wallet = True
    if has_wallet:
        badges.append("wallet")
    if user.is_staff:
        badges.append("staff")
    if not user.is_active:
        badges.append("disabled")
    return badges


def serialize_user_list_item(user: User) -> dict[str, Any]:
    account = getattr(user, "billing_account", None)
    balance = account.balance if account is not None else None
    return {
        "id": user.pk,
        "email": user.email,
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "last_login": (
            user.last_login.isoformat().replace("+00:00", "Z")
            if user.last_login
            else None
        ),
        "balance": f"{balance:.6f}" if balance is not None else None,
        "badges": user_badges(user),
    }


def _wallet_section(user: User) -> dict[str, Any] | None:
    account = getattr(user, "billing_account", None)
    if account is None:
        return None
    identity = getattr(account, "wallet_identity", None)
    addresses: list[dict[str, Any]] = []
    if identity is not None:
        for addr in identity.addresses.all():
            addresses.append(
                {
                    "id": str(addr.pk),
                    "chain": addr.chain,
                    "address": addr.address,
                    "status": addr.status,
                    "derivation_index": addr.derivation_index,
                    "created_at": addr.created_at.isoformat().replace("+00:00", "Z"),
                }
            )

    deposits = DepositRequest.objects.filter(
        account=account,
        status=DepositRequest.Status.COMPLETED,
    )
    totals = deposits.aggregate(total=Sum("amount_credited"))
    last = deposits.order_by("-verified_at", "-created_at").first()
    total_deposited = totals["total"] or Decimal("0")

    return {
        "wallet_age": (
            identity.created_at.isoformat().replace("+00:00", "Z") if identity else None
        ),
        "addresses": addresses,
        "has_completed_deposit": last is not None,
        "last_deposit_at": (
            (last.verified_at or last.created_at).isoformat().replace("+00:00", "Z")
            if last
            else None
        ),
        "last_tx_hash": last.tx_hash if last else None,
        "total_deposited": f"{total_deposited:.6f}",
    }


def serialize_user_detail(user: User) -> dict[str, Any]:
    account = getattr(user, "billing_account", None)
    esims = list(
        Esim.objects.filter(user=user)
        .order_by("-created_at")
        .values("id", "iccid", "status", "order_id", "created_at")
    )
    esim_payload = [
        {
            "id": row["id"],
            "iccid": row["iccid"],
            "status": row["status"],
            "status_label": ESIM_STATUS_LABELS.get(row["status"], row["status"]),
            "order_id": row["order_id"],
            "created_at": row["created_at"].isoformat().replace("+00:00", "Z"),
        }
        for row in esims
    ]

    ua = (
        EsimLifecycleEvent.objects.filter(user=user)
        .exclude(user_agent="")
        .order_by("-created_at")
        .values_list("user_agent", flat=True)
        .first()
    )

    return {
        "schema_version": USER_DETAIL_SCHEMA_VERSION,
        "id": user.pk,
        "email": user.email,
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "created_at": user.created_at.isoformat().replace("+00:00", "Z"),
        "last_login": (
            user.last_login.isoformat().replace("+00:00", "Z")
            if user.last_login
            else None
        ),
        "badges": user_badges(user),
        "account": (
            {
                "id": str(account.pk),
                "balance": f"{account.balance:.6f}",
                "version": account.version,
            }
            if account is not None
            else None
        ),
        "esims": esim_payload,
        "wallet": _wallet_section(user),
        "device_hints": {"user_agent": ua or ""},
        "timeline": build_user_timeline(user, limit=100),
    }


def users_queryset():
    return User.objects.select_related(
        "billing_account",
        "billing_account__wallet_identity",
    ).prefetch_related(
        Prefetch(
            "billing_account__wallet_identity__addresses",
            queryset=WalletAddress.objects.order_by("derivation_index"),
        )
    )


# Whitelisted list sorts (DRF-style ``ordering`` query param).
_USER_LIST_ORDERING: dict[str, tuple[str, ...]] = {
    "email": ("email",),
    "-email": ("-email",),
    "balance": ("billing_account__balance", "email"),
    "-balance": ("-billing_account__balance", "email"),
    "last_login": ("last_login", "email"),
    "-last_login": ("-last_login", "email"),
    # Flags are derived badges; approximate with staff then active.
    "flags": ("is_staff", "is_active", "email"),
    "-flags": ("-is_staff", "-is_active", "email"),
}

_DEFAULT_USER_LIST_ORDERING = ("-created_at",)


def apply_user_list_ordering(qs, ordering: str | None):
    """Apply a whitelisted ``ordering`` value; unknown values keep default."""
    key = (ordering or "").strip()
    fields = _USER_LIST_ORDERING.get(key, _DEFAULT_USER_LIST_ORDERING)
    return qs.order_by(*fields)
