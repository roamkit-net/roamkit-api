"""Rebuild Account.balance cache from ledger SUM (explicit ops action)."""

from django.core.management.base import BaseCommand

from apps.billing.models import Account
from apps.billing.services import credit_service, rebuild_service


class Command(BaseCommand):
    help = (
        "Rebuild Account.balance from SUM(CreditLedgerEntry.delta). "
        "Does not auto-run in production reconcile — explicit operator action only."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--account-id",
            help="Rebuild a single account UUID (default: all accounts).",
        )

    def handle(self, *args, **options) -> None:
        account_id = options.get("account_id")
        if account_id:
            account = Account.objects.get(pk=account_id)
            before = account.balance
            after = credit_service.rebuild_balance_from_ledger(account)
            self.stdout.write(
                self.style.SUCCESS(f"Account {account.pk}: {before} → {after}")
            )
            return

        stats = rebuild_service.rebuild_all()
        self.stdout.write(
            self.style.SUCCESS(
                f"Checked {stats['checked']} account(s); "
                f"repaired {stats['repaired']}."
            )
        )
