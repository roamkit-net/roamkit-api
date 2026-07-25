"""Post-migrate assertions for the pre-billing → HEAD migration smoke dump."""

from __future__ import annotations

import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from django.db import connection  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.billing.models import Account  # noqa: E402
from apps.orders.models import Order  # noqa: E402


def main() -> int:
    users = User.objects.count()
    accounts = Account.objects.count()
    orders = Order.objects.count()

    print(f"users={users} accounts={accounts} orders={orders}")

    if users < 3:
        print("FAIL: expected at least 3 users from smoke dump", file=sys.stderr)
        return 1
    if accounts != users:
        print(
            f"FAIL: every user must have an Account ({accounts=} {users=})",
            file=sys.stderr,
        )
        return 1
    if orders < 7:
        print("FAIL: expected at least 7 orders from smoke dump", file=sys.stderr)
        return 1

    orphan_orders = Order.objects.filter(account__isnull=True).count()
    if orphan_orders:
        print(f"FAIL: {orphan_orders} orders without account", file=sys.stderr)
        return 1

    # Order.user must be gone; account.user must resolve for every order.
    field_names = {f.name for f in Order._meta.fields}
    if "user" in field_names:
        print("FAIL: Order.user is still present", file=sys.stderr)
        return 1
    if "account" not in field_names:
        print("FAIL: Order.account is missing", file=sys.stderr)
        return 1

    mismatched = 0
    for order in Order.objects.select_related("account__user").all():
        if order.account is None or order.account.user_id is None:
            mismatched += 1
    if mismatched:
        print(f"FAIL: {mismatched} orders with broken account.user", file=sys.stderr)
        return 1

    statuses = set(Order.objects.values_list("status", flat=True))
    expected = {
        Order.Status.DRAFT,
        Order.Status.PENDING_PAYMENT,
        Order.Status.PAID,
        Order.Status.FULFILLING,
        Order.Status.FULFILLED,
        Order.Status.FAILED,
        Order.Status.CANCELLED,
    }
    missing = expected - statuses
    if missing:
        print(f"FAIL: missing order statuses after migrate: {missing}", file=sys.stderr)
        return 1

    # Confirm DB column shape (user_id dropped, account_id present).
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'orders_order'
            """
        )
        columns = {row[0] for row in cur.fetchall()}
    if "user_id" in columns:
        print("FAIL: orders_order.user_id column still exists", file=sys.stderr)
        return 1
    if "account_id" not in columns:
        print("FAIL: orders_order.account_id column missing", file=sys.stderr)
        return 1

    # Spot-check known dump rows: fulfilled order 5 belonged to bob (user 2).
    order5 = Order.objects.select_related("account__user").get(pk=5)
    if order5.account.user.email != "bob@example.com":
        print(
            f"FAIL: order 5 expected bob@example.com, got {order5.account.user.email}",
            file=sys.stderr,
        )
        return 1
    if order5.status != Order.Status.FULFILLED:
        print(
            f"FAIL: order 5 status expected fulfilled, got {order5.status}",
            file=sys.stderr,
        )
        return 1

    print("OK: migration smoke verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
