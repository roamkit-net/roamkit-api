"""ASGI config for roamkit-api."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.staging")

application = get_asgi_application()
