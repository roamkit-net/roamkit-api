"""Batch WalletAddress backfill + Data Migration Validation (ADR 018 Phase 0)."""

from django.core.management.base import BaseCommand, CommandError

from apps.wallet.models import WalletChain
from apps.wallet.services.backfill import run_wallet_backfill, validate_wallet_migration


class Command(BaseCommand):
    help = (
        "ADR 018 Data Migration Gate: dry-run / apply batch pre-allocate of "
        "WalletIdentity + active Polygon WalletAddress for Accounts that lack "
        "one, then optionally run Validation. Does not mutate Credits or "
        "deposit-info."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Allocate missing addresses (default: dry-run report only).",
        )
        parser.add_argument(
            "--validate",
            action="store_true",
            help="Run Data Migration Validation after (or instead of) backfill.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Max Accounts to allocate (oldest first).",
        )
        parser.add_argument(
            "--sample-size",
            type=int,
            default=10,
            help="Addresses to re-derive when --validate (default: 10).",
        )
        parser.add_argument(
            "--chain",
            default=WalletChain.POLYGON,
            help="Chain for active address (default: polygon).",
        )

    def handle(self, *args, **options) -> None:
        apply = bool(options["apply"])
        validate = bool(options["validate"])
        limit = options["limit"]
        sample_size = int(options["sample_size"])
        chain = str(options["chain"])

        if limit is not None and limit < 1:
            raise CommandError("--limit must be >= 1")
        if sample_size < 0:
            raise CommandError("--sample-size must be >= 0")

        if apply or not validate:
            report = run_wallet_backfill(apply=apply, limit=limit, chain=chain)
            data = report.as_dict()
            self.stdout.write(
                f"backfill mode={data['mode']} chain={data['chain']} "
                f"scanned={data['accounts_scanned']} "
                f"already_ready={data['already_ready']} "
                f"would_allocate={data['would_allocate']} "
                f"allocated={data['allocated']} errors={data['errors']}"
            )
            for detail in data["error_details"]:
                self.stdout.write(self.style.ERROR(detail))
            if data["errors"]:
                raise CommandError(f"{data['errors']} backfill error(s)")

        if validate:
            vreport = validate_wallet_migration(
                chain=chain,
                sample_size=sample_size,
            )
            vdata = vreport.as_dict()
            status = "PASS" if vdata["passed"] else "FAIL"
            style = self.style.SUCCESS if vdata["passed"] else self.style.ERROR
            self.stdout.write(
                style(
                    f"validation={status} "
                    f"accounts_total={vdata['accounts_total']} "
                    f"missing_identity={vdata['accounts_missing_identity']} "
                    f"missing_active={vdata['accounts_missing_active_address']} "
                    f"orphan_identities={vdata['orphan_identities']} "
                    f"duplicate_active={vdata['duplicate_active_addresses']} "
                    f"missing_index_fields={vdata['missing_index_registry_fields']} "
                    f"sample_checked={vdata['sample_checked']} "
                    f"sample_mismatches={vdata['sample_mismatches']}"
                )
            )
            for err in vdata["sample_errors"]:
                self.stdout.write(self.style.ERROR(err))
            if not vdata["passed"]:
                raise CommandError("Data Migration Validation failed")
