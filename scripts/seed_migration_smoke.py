"""Seed the pre-billing migration-smoke database.

Used by build_migration_smoke_dump.sh.
"""

from __future__ import annotations

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from datetime import UTC, datetime  # noqa: E402

from django.contrib.auth.hashers import make_password  # noqa: E402
from django.db import connection  # noqa: E402

now = datetime.now(UTC).isoformat()
password = make_password("SmokePass1!")

with connection.cursor() as cur:
    cur.execute(
        """
        INSERT INTO accounts_user
          (id, password, last_login, is_superuser, email, is_staff, is_active,
           created_at, updated_at)
        VALUES
          (1, %s, NULL, false, 'alice@example.com', false, true, %s, %s),
          (2, %s, NULL, false, 'bob@example.com', false, true, %s, %s),
          (3, %s, NULL, false, 'inactive@example.com', false, false, %s, %s)
        """,
        [password, now, now, password, now, now, password, now, now],
    )
    cur.execute(
        """
        INSERT INTO catalog_package
          (id, external_id, title, operator_title, operator_id, country_code,
           data_allowance, validity_days, price_usd, net_price_usd, is_unlimited,
           plan_type, source, is_active, synced_at, created_at, updated_at,
           voice_minutes, text_sms, location_id)
        VALUES
          (1, 'pkg-us-1gb', '1 GB - 7 Days', 'Change', '', 'US',
           '1 GB', 7, 11.50, 10.00, false,
           'data', 'airalo', true, %s, %s, %s,
           NULL, NULL, NULL)
        """,
        [now, now, now],
    )
    orders = [
        (1, "draft", "", "ref-draft", 1),
        (2, "pending_payment", "", "ref-pending", 1),
        (3, "paid", "ext-paid-1", "ref-paid", 1),
        (4, "fulfilling", "ext-fulfilling-1", "ref-fulfilling", 2),
        (5, "fulfilled", "ext-fulfilled-1", "ref-fulfilled", 2),
        (6, "failed", "ext-failed-1", "ref-failed", 2),
        (7, "cancelled", "", "ref-cancelled", 1),
    ]
    for oid, status, ext, cref, uid in orders:
        cur.execute(
            """
            INSERT INTO orders_order
              (id, status, external_order_id, customer_ref, created_at, updated_at,
               package_id, user_id)
            VALUES (%s, %s, %s, %s, %s, %s, 1, %s)
            """,
            [oid, status, ext, cref, now, now, uid],
        )
    cur.execute(
        """
        INSERT INTO esims_esim
          (id, iccid, lpa, matching_id, qrcode, qrcode_url,
           direct_apple_installation_url, manual_installation, qrcode_installation,
           installation_guide_url, status, usage_remaining_mb, usage_total_mb,
           usage_status, usage_is_unlimited, usage_expired_at, usage_synced_at,
           created_at, updated_at, order_id, user_id)
        VALUES
          (1, '891000000000009125', 'lpa.airalo.com', 'TEST',
           'LPA:1$lpa.airalo.com$TEST', '',
           '', '', '',
           '', 'unused', NULL, NULL,
           '', NULL, NULL, NULL,
           %s, %s, 5, 2)
        """,
        [now, now],
    )
    cur.execute(
        """
        SELECT setval(pg_get_serial_sequence('accounts_user', 'id'),
                      (SELECT MAX(id) FROM accounts_user));
        SELECT setval(pg_get_serial_sequence('catalog_package', 'id'),
                      (SELECT MAX(id) FROM catalog_package));
        SELECT setval(pg_get_serial_sequence('orders_order', 'id'),
                      (SELECT MAX(id) FROM orders_order));
        SELECT setval(pg_get_serial_sequence('esims_esim', 'id'),
                      (SELECT MAX(id) FROM esims_esim));
        """
    )

print("Seeded: 3 users, 1 package, 7 orders, 1 esim")
