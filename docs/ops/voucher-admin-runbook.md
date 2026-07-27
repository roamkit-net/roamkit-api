# Voucher admin runbook (PR2)

## 1. Create campaign

1. Open Django admin → Billing → Voucher campaigns → Add.
2. Prefer status **Draft**, set shared `code` (if SHARED), credit amount, limits, expiry.
3. Save. Issuer fields are set automatically for admin users.

## 2. Generate batch

1. On the campaign row, click **Generate…**
2. Review preview (size, reward, amount, expiry, `RK-` prefix, irreversible warning).
3. Check confirmation and submit.
4. If you see “Batch generation already in progress” or a conflict, reload and retry.

## 3. Export PDF

On the batch change page / list: **PDF** (records export audit).

## 4. Export CSV

**CSV** for real download (audit). **Preview CSV** does not write audit rows.

## 5. Activate

Select draft campaigns → action **Activate selected campaigns** (requires `issue_voucher`).

## 6. Revoke

Use revoke confirm screens (campaign / batch / selected vouchers). Provide
`revoke_reason`. Review impact preview, then confirm. Post-action message shows
affected / already redeemed / revoked now. Redeemed unique vouchers are never
revoked.

## 7. Extend expiry

Use extend actions on campaigns or vouchers that are not redeemed/revoked.

## 8. Recovery

| Situation | Action |
|-----------|--------|
| Optimistic concurrency conflict | Reload the form and retry |
| Batch generation busy | Wait for the other transaction; retry |
| Mistaken issuance | **Revoke** (never hard-delete) |
| Redeem API 404 | Enable `VOUCHERS_ENABLED` (and `BILLING_ENABLED`) |
| Accidental export | Audit row remains; revoke codes if needed |

Hard delete is blocked on models, querysets, and admin.
