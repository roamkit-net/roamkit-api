"""Global ops search across users, orders, deposits, eSIMs (and vouchers)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.contrib.auth import get_user_model
from django.db.models import Q

from apps.billing.models import DepositRequest, Voucher
from apps.esims.models import Esim
from apps.orders.models import Order
from apps.wallet.models import WalletAddress

User = get_user_model()

SEARCH_SCHEMA_VERSION = 1
PER_TYPE_LIMIT = 10
MIN_QUERY_LEN = 3


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except (TypeError, ValueError):
        return False
    return True


def search_ops(query: str) -> dict[str, Any]:
    """Return grouped search hits. Empty groups stay present for a stable contract."""
    q = (query or "").strip()
    empty = {
        "schema_version": SEARCH_SCHEMA_VERSION,
        "query": q,
        "users": [],
        "orders": [],
        "deposits": [],
        "esims": [],
        "vouchers": [],
    }
    if not q:
        return empty
    # Allow short exact UUID / long ICCID-like tokens under min length.
    if len(q) < MIN_QUERY_LEN and not _is_uuid(q):
        return empty

    users: list[dict[str, Any]] = []
    user_ids: set[int] = set()

    for user in User.objects.filter(email__icontains=q).order_by("email")[
        :PER_TYPE_LIMIT
    ]:
        users.append(
            {
                "id": user.pk,
                "label": user.email,
                "match": "email",
            }
        )
        user_ids.add(user.pk)

    for user in (
        User.objects.filter(wallet_address__icontains=q)
        .exclude(pk__in=user_ids)
        .order_by("email")[:PER_TYPE_LIMIT]
    ):
        users.append(
            {
                "id": user.pk,
                "label": user.email,
                "match": "wallet",
            }
        )
        user_ids.add(user.pk)

    for addr in (
        WalletAddress.objects.filter(address__icontains=q)
        .select_related("wallet_identity__account__user")
        .order_by("address")[:PER_TYPE_LIMIT]
    ):
        user = addr.wallet_identity.account.user
        if user.pk in user_ids:
            continue
        users.append(
            {
                "id": user.pk,
                "label": user.email,
                "match": "wallet_address",
            }
        )
        user_ids.add(user.pk)
        if len(users) >= PER_TYPE_LIMIT:
            break

    orders: list[dict[str, Any]] = []
    order_q = Q(external_order_id__icontains=q) | Q(account__user__email__icontains=q)
    if q.isdigit():
        order_q |= Q(pk=int(q))
    if _is_uuid(q):
        # orders use int pk today; keep branch for future uuid ids
        pass
    for order in (
        Order.objects.filter(order_q)
        .select_related("account__user")
        .order_by("-created_at")[:PER_TYPE_LIMIT]
    ):
        orders.append(
            {
                "id": order.pk,
                "label": order.package_title or f"Order {order.pk}",
                "status": order.status,
                "user_id": order.account.user_id,
                "user_email": order.account.user.email,
                "match": "order",
            }
        )

    deposits: list[dict[str, Any]] = []
    deposit_q = Q(tx_hash__icontains=q) | Q(account__user__email__icontains=q)
    if _is_uuid(q):
        deposit_q |= Q(pk=q)
    for deposit in (
        DepositRequest.objects.filter(deposit_q)
        .select_related("account__user")
        .order_by("-created_at")[:PER_TYPE_LIMIT]
    ):
        deposits.append(
            {
                "id": str(deposit.pk),
                "label": deposit.tx_hash or str(deposit.pk),
                "status": deposit.status,
                "user_id": deposit.account.user_id,
                "user_email": deposit.account.user.email,
                "match": "deposit",
            }
        )

    esims: list[dict[str, Any]] = []
    esim_q = Q(iccid__icontains=q) | Q(user__email__icontains=q)
    if q.isdigit():
        esim_q |= Q(pk=int(q))
    for esim in (
        Esim.objects.filter(esim_q)
        .select_related("user")
        .order_by("-created_at")[:PER_TYPE_LIMIT]
    ):
        esims.append(
            {
                "id": esim.pk,
                "label": esim.iccid,
                "status": esim.status,
                "user_id": esim.user_id,
                "user_email": esim.user.email,
                "match": "iccid",
            }
        )

    vouchers: list[dict[str, Any]] = []
    for voucher in Voucher.objects.filter(code__icontains=q).order_by("code")[
        :PER_TYPE_LIMIT
    ]:
        vouchers.append(
            {
                "id": str(voucher.pk),
                "label": voucher.code,
                "status": voucher.status,
                "match": "voucher",
            }
        )

    return {
        "schema_version": SEARCH_SCHEMA_VERSION,
        "query": q,
        "users": users[:PER_TYPE_LIMIT],
        "orders": orders,
        "deposits": deposits,
        "esims": esims,
        "vouchers": vouchers,
    }
