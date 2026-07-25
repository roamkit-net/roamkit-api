from django.apps import AppConfig


class EsimsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.esims"
    label = "esims"

    def ready(self) -> None:
        from apps.billing.models import REFERENCE_MODELS, LedgerReferenceType
        from apps.esims.models import Topup

        REFERENCE_MODELS[LedgerReferenceType.TOPUP] = Topup
