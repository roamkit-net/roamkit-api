from django.apps import AppConfig


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.billing"
    label = "billing"

    def ready(self) -> None:
        from apps.billing import signals  # noqa: F401
        from apps.billing.models import REFERENCE_MODELS, LedgerReferenceType
        from apps.orders.models import Order

        REFERENCE_MODELS[LedgerReferenceType.ORDER] = Order
