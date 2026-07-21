"""Local development settings."""

from .base import *  # noqa: F403

DEBUG = True

ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

# Prefer console locally unless EMAIL_HOST points at Mailpit/SMTP.
if not EMAIL_HOST:  # noqa: F405
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
