"""Celery application for roamkit-api."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.staging")

app = Celery("roamkit")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
