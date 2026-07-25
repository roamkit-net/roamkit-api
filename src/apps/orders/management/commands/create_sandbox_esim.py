"""Create a sandbox eSIM for a user via the configured OrderProvider."""

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.catalog.models import Package
from apps.orders.services.order_service import OrderService
from shared.providers.factory import get_order_provider


class Command(BaseCommand):
    help = (
        "Create a sandbox eSIM for a user by placing a provider order "
        "and persisting Order + Esim rows."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--email",
            required=True,
            help="Email of an existing user to attach the eSIM to.",
        )
        parser.add_argument(
            "--package-id",
            required=True,
            help="Catalog package external_id (Airalo package id).",
        )

    def handle(self, *args, **options) -> None:
        email = options["email"]
        package_id = options["package_id"]

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist as exc:
            raise CommandError(f"User with email {email!r} not found.") from exc

        try:
            package = Package.objects.get(external_id=package_id)
        except Package.DoesNotExist as exc:
            raise CommandError(
                f"Package with external_id {package_id!r} not found. "
                "Run sync_packages first."
            ) from exc

        service = OrderService(get_order_provider())
        order = service.fulfill(
            user=user,
            package=package,
            customer_ref=f"sandbox:{user.email}",
            skip_payment=True,
        )

        iccids = list(order.esims.values_list("iccid", flat=True))
        self.stdout.write(
            self.style.SUCCESS(
                f"Order {order.pk} fulfilled "
                f"(external_order_id={order.external_order_id!r}). "
                f"ICCID(s): {', '.join(iccids) or '(none)'}"
            )
        )
