# Credit vouchers (ADR 011) — PR1

## Compatibility guarantee

PR1 does **not** change existing billing endpoint semantics or `CreditService`.
The only new public surface is:

```http
POST /api/v1/billing/vouchers/redeem/
```

## Feature flag

| Env | Default | Effect |
|-----|---------|--------|
| `VOUCHERS_ENABLED` | `false` | When false (or `BILLING_ENABLED=false`), redeem returns **404** |

## Sequence

```text
User → API (request_id + throttle)
    → VoucherRedeemService
        BEGIN
          lock Voucher|Campaign
          validate
          INSERT VoucherRedemption
          CreditService.credit (Account → Ledger)
        COMMIT
    → VoucherRedeemed event
```

## Definition of Done

- [x] Model / service / API / concurrency / torture / arch tests
- [x] OpenAPI regenerated (`openapi/openapi.yaml`)
- [x] No admin UI, no web redeem UI
- [x] ADR 011 / ADR 012 invariants preserved (`CreditService` only money mutator)

Admin issuance and web UI are **PR2 / PR3**.
