"""Compare Account.balance to ledger SUM and alert on drift (no auto-fix)."""

from django.core.management.base import BaseCommand

from apps.billing.services import reconcile_service


class Command(BaseCommand):
    help = (
        "Reconcile Account.balance cache vs ledger SUM. "
        "Alerts on drift; does not mutate balances (use billing_rebuild_balances)."
    )

    def handle(self, *args, **options) -> None:
        stats = reconcile_service.reconcile()
        style = self.style.ERROR if stats["drifts"] else self.style.SUCCESS
        self.stdout.write(
            style(
                f"Checked {stats['checked']} account(s); "
                f"found {stats['drifts']} drift(s)."
            )
        )
