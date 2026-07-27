# Credit vouchers (ADR 011) — PR2 Django admin

## Scope

Operater tools only. No new public billing endpoints. Redeem semantics remain PR1
(`VoucherRedeemService` → `CreditService`).

## Feature flags

| Env | Effect |
|-----|--------|
| `VOUCHERS_ENABLED` | End-user redeem API; admin may still issue/revoke when false (warnings shown) |
| `BILLING_ENABLED` | Required for redeem |

## Permissions (Django)

Codename on `billing.Voucher`:

| Permission | Allows |
|------------|--------|
| `view_voucher` / `view_vouchercampaign` / … | Browse |
| `issue_voucher` | Create/activate campaigns, generate batches, extend expiry |
| `revoke_voucher` | Revoke campaign / batch / vouchers |
| `export_voucher` | CSV/PDF download + preview |

Grant via Django admin Groups or `user.user_permissions`.

## Concurrency

- Mutating ops accept `expected_updated_at` (optimistic concurrency).
- Batch generate takes campaign row `FOR UPDATE NOWAIT` + advisory lock.

## Export

- Batch **CSV** and **PDF** re-download (minimal PDF writer, no extra deps).
- Selected voucher CSV export.
- Preview downloads do not write `VoucherExportAudit`.

## Ops runbook

See [ops/voucher-admin-runbook.md](ops/voucher-admin-runbook.md).

## Deferred

- Code hash column
- Streaming HTTP for 100k+ rows (service already iterates)
- Next.js ops portal
