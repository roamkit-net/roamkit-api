"""Resume Confirmed Observations → Credits (ADR 017 recovery drill)."""

from django.core.management.base import BaseCommand, CommandError

from apps.wallet.services.ops import resume_converts


class Command(BaseCommand):
    help = (
        "Resume Credit Conversion for Confirmed / Conversion Started "
        "observations. Default is dry-run; pass --apply to call CreditService."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Perform CreditConversionService.convert (default: dry-run).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Max observations to process (oldest first).",
        )

    def handle(self, *args, **options) -> None:
        apply = bool(options["apply"])
        limit = options["limit"]
        if limit is not None and limit < 1:
            raise CommandError("--limit must be >= 1")

        report = resume_converts(apply=apply, limit=limit)
        mode = "apply" if apply else "dry-run"
        self.stdout.write(
            f"mode={mode} candidates={report['candidates']} "
            f"credited={report['credited']} errors={report['errors']}"
        )
        for row in report["results"]:
            line = (
                f"{row['action']} id={row['id']} status={row['status']} "
                f"identity={row['identity']}"
            )
            if row.get("ledger_entry_id"):
                line += f" ledger={row['ledger_entry_id']}"
            if row.get("error"):
                line += f" error={row['error']}"
            style = self.style.ERROR if row["action"] == "error" else self.style.NOTICE
            self.stdout.write(style(line))

        if report["errors"]:
            raise CommandError(f"{report['errors']} convert error(s)")
