"""Billing domain constants (ADR 010).

Keep ledger/display currency in one place so order snapshots and future
multi-currency work do not scatter literals across the codebase.
"""

# Prepaid credits are 1:1 with this ISO 4217 code today (ADR 010).
LEDGER_CURRENCY = "USD"
