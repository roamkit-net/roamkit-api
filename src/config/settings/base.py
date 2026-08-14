"""Shared Django settings for roamkit-api."""

import os
from datetime import timedelta
from pathlib import Path

from corsheaders.defaults import default_headers

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "change-me-local-dev-only")

DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt",
    "drf_spectacular",
    "core.apps.CoreConfig",
    "apps.accounts",
    "apps.billing",
    "apps.pricing",
    "apps.wallet",
    "apps.organizations",
    "apps.catalog",
    "apps.orders",
    "apps.esims",
    "apps.notifications",
    "apps.integrations",
    "apps.ops",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.request_id.RequestIdMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "roamkit"),
        "USER": os.environ.get("POSTGRES_USER", "roamkit"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "roamkit"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
        ),
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"

CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "https://staging.roamkit.net",
    "https://roamkit.net",
]

# Browser clients send headers that django-cors-headers defaults omit.
# Missing entries → OPTIONS preflight fails → "Failed to fetch" in the web app.
# - x-request-id: voucher redeem / request tracing
# - if-match: auto top-up optimistic concurrency (me API PUT/DELETE)
CORS_ALLOW_HEADERS = (
    *default_headers,
    "x-request-id",
    "if-match",
)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# LocMem by default (local/tests). Staging/production override to Redis so
# DRF throttles and Turnstile replay keys are shared across workers.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "roamkit-default",
    }
}

CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULE = {
    "sync-airalo-packages-hourly": {
        "task": "catalog.sync_airalo_packages",
        "schedule": 3600.0,
    },
    "billing-renew-subscriptions-daily": {
        "task": "billing.renew_subscriptions",
        "schedule": 86400.0,
    },
    "billing-reconcile-balances-daily": {
        "task": "billing.reconcile_balances",
        "schedule": 86400.0,
    },
    "esims-evaluate-auto-topups": {
        "task": "esims.evaluate_auto_topups",
        "schedule": 300.0,
    },
}

AUTH_TOKEN_RATE = os.environ.get("AUTH_TOKEN_RATE", "10/min")
AUTH_REGISTER_RATE = os.environ.get("AUTH_REGISTER_RATE", "5/hour")
AUTH_PASSWORD_RESET_RATE = os.environ.get("AUTH_PASSWORD_RESET_RATE", "5/hour")
AUTH_ACTIVATE_RATE = os.environ.get("AUTH_ACTIVATE_RATE", "20/hour")
AUTH_PASSWORD_RESET_CONFIRM_RATE = os.environ.get(
    "AUTH_PASSWORD_RESET_CONFIRM_RATE", "20/hour"
)
AUTH_GOOGLE_RATE = os.environ.get("AUTH_GOOGLE_RATE", "10/min")
AUTH_TURNSTILE_DEGRADED_RATE = os.environ.get("AUTH_TURNSTILE_DEGRADED_RATE", "5/hour")
BILLING_VOUCHER_REDEEM_RATE = os.environ.get("BILLING_VOUCHER_REDEEM_RATE", "10/5min")
DEVICE_STATUS_RATE = os.environ.get("DEVICE_STATUS_RATE", "60/hour")
DEVICE_COVERAGE_RATE = os.environ.get("DEVICE_COVERAGE_RATE", "60/hour")
DEVICE_PACKAGES_RATE = os.environ.get("DEVICE_PACKAGES_RATE", "60/hour")
PUBLIC_ESIM_STATUS_RATE = os.environ.get("PUBLIC_ESIM_STATUS_RATE", "60/hour")

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_RATES": {
        "auth_token": AUTH_TOKEN_RATE,
        "auth_register": AUTH_REGISTER_RATE,
        "auth_password_reset": AUTH_PASSWORD_RESET_RATE,
        "auth_activate": AUTH_ACTIVATE_RATE,
        "auth_password_reset_confirm": AUTH_PASSWORD_RESET_CONFIRM_RATE,
        "auth_google": AUTH_GOOGLE_RATE,
        "billing_voucher_redeem": BILLING_VOUCHER_REDEEM_RATE,
        "device_status": DEVICE_STATUS_RATE,
        "device_coverage": DEVICE_COVERAGE_RATE,
        "device_packages": DEVICE_PACKAGES_RATE,
        "public_esim_status": PUBLIC_ESIM_STATUS_RATE,
    },
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# OpenAPI (C10) — generate only via scripts/generate_openapi.sh
SPECTACULAR_SETTINGS = {
    "TITLE": "RoamKit API",
    "DESCRIPTION": (
        "Self-service eSIM API. Public REST under `/api/v1/`. "
        "Staff Operations Dashboard under `/api/v1/admin/` (read-only). "
        "Staff internal tools under `/api/internal/`. "
        "Authenticate with JWT Bearer tokens from `/api/v1/auth/token/`."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SORT_OPERATIONS": True,
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": r"/api/v1",
    "TAGS": [
        {
            "name": "Authentication",
            "description": "Registration, login, and password reset",
        },
        {
            "name": "Billing",
            "description": "Prepaid credits, deposits, and wallet balance",
        },
        {
            "name": "Vouchers",
            "description": "Credit voucher and gift-code redeem (ADR 011)",
        },
        {"name": "Orders", "description": "Package purchase orders"},
        {"name": "Catalog", "description": "Packages and locations"},
        {"name": "eSIM", "description": "User eSIM inventory, usage, and top-ups"},
        {
            "name": "Organizations",
            "description": (
                "Team organizations and memberships (ADR 020). "
                "Authorize via organization_id; never via client account_id."
            ),
        },
        {
            "name": "Device",
            "description": (
                "Device-facing endpoints authenticated by opaque device "
                "credential (not user JWT). Used by managed devices / UEM."
            ),
        },
        {"name": "Users", "description": "Authenticated user profile"},
        {
            "name": "Ops",
            "description": (
                "Staff-only read-only Operations Dashboard "
                "(members, search, aggregates)"
            ),
        },
        {
            "name": "Internal",
            "description": (
                "Staff-only internal tools under `/api/internal/` "
                "(pricing preview; not public catalog)"
            ),
        },
    ],
    "SERVERS": [
        {"url": "https://api.staging.roamkit.net", "description": "Staging"},
        {"url": "https://api.roamkit.net", "description": "Production"},
    ],
    "SECURITY": [{"bearerAuth": []}],
    "ENUM_NAME_OVERRIDES": {
        "OrderStatusEnum": "apps.orders.models.Order.Status",
        "DepositRequestStatusEnum": "apps.billing.models.DepositRequest.Status",
        "DepositPaymentMethodEnum": "apps.billing.models.DepositRequest.PaymentMethod",
        "LedgerReferenceTypeEnum": "apps.billing.models.LedgerReferenceType",
        "InviteRoleEnum": "apps.organizations.models.InviteRole",
        "MembershipRoleEnum": "apps.organizations.models.MembershipRole",
    },
}

PACKAGE_PROVIDER = "apps.integrations.airalo.providers.AiraloPackageProvider"
ORDER_PROVIDER = "apps.integrations.airalo.providers.AiraloOrderProvider"
TOPUP_PROVIDER = "apps.integrations.airalo.providers.AiraloTopupProvider"
BLOCKCHAIN_PROVIDER = os.environ.get(
    "BLOCKCHAIN_PROVIDER",
    "apps.integrations.polygon.providers.PolygonProvider",
)
MEXC_FUNDING_PROVIDER = os.environ.get(
    "MEXC_FUNDING_PROVIDER",
    "apps.integrations.mexc.providers.MexcFundingProvider",
)
MEXC_WITHDRAW_URL = os.environ.get(
    "MEXC_WITHDRAW_URL",
    "https://www.mexc.com/assets/withdraw/USDT",
)
MEXC_POLYGON_NETWORK_LABEL = os.environ.get("MEXC_POLYGON_NETWORK_LABEL", "MATIC")

BINANCE_FUNDING_PROVIDER = os.environ.get(
    "BINANCE_FUNDING_PROVIDER",
    "apps.integrations.binance.providers.BinanceFundingProvider",
)
BINANCE_WITHDRAW_URL = os.environ.get(
    "BINANCE_WITHDRAW_URL",
    "https://www.binance.com/en/my/wallet/account/main/withdrawal/crypto/USDT",
)
BINANCE_POLYGON_NETWORK_LABEL = os.environ.get("BINANCE_POLYGON_NETWORK_LABEL", "MATIC")

AIRALO_CLIENT_ID = os.environ.get("AIRALO_CLIENT_ID", "")
AIRALO_CLIENT_SECRET = os.environ.get("AIRALO_CLIENT_SECRET", "")
AIRALO_SANDBOX = os.environ.get("AIRALO_SANDBOX", "true").lower() == "true"
AIRALO_BASE_URL = os.environ.get("AIRALO_BASE_URL", "https://partners-api.airalo.com")
# Default true for local/dev convenience; staging/production enforce via guards.
AIRALO_ENABLED = os.environ.get("AIRALO_ENABLED", "true").lower() == "true"
# Comma-separated denylist (staging). Never commit Fine Star live client_id.
AIRALO_BLOCKED_CLIENT_IDS = os.environ.get("AIRALO_BLOCKED_CLIENT_IDS", "")

# BlackBerry UEM Cloud REST (ADR 021 option C staging proof — read-only).
# Default off: classic PR18 device status ignores UEM until explicitly enabled
# and DeviceBinding.uem_device_guid is set (manual/admin).
BLACKBERRY_UEM_ENABLED = (
    os.environ.get("BLACKBERRY_UEM_ENABLED", "false").lower() == "true"
)
BLACKBERRY_UEM_TENANT_ID = os.environ.get("BLACKBERRY_UEM_TENANT_ID", "")
BLACKBERRY_UEM_HOST = os.environ.get("BLACKBERRY_UEM_HOST", "")
BLACKBERRY_UEM_CLIENT_ID = os.environ.get("BLACKBERRY_UEM_CLIENT_ID", "")
BLACKBERRY_UEM_CLIENT_SECRET = os.environ.get("BLACKBERRY_UEM_CLIENT_SECRET", "")
BLACKBERRY_UEM_TOKEN_URL = os.environ.get("BLACKBERRY_UEM_TOKEN_URL", "")
BLACKBERRY_UEM_SCOPE = os.environ.get("BLACKBERRY_UEM_SCOPE", "openid MDMBWS.All")
BLACKBERRY_UEM_TIMEOUT = float(os.environ.get("BLACKBERRY_UEM_TIMEOUT", "30"))

# ADR 021 Option C′ — fleet credential previous-secret grace window (seconds).
# Default 72h so UEM App Configuration can roll out before old secret dies.
FLEET_CREDENTIAL_GRACE_SECONDS = int(
    os.environ.get("FLEET_CREDENTIAL_GRACE_SECONDS", str(72 * 60 * 60))
)

# Billing feature flags (ADR-010). Money endpoints under /api/v1/billing/*
# return 404 when BILLING_ENABLED is false; GET …/billing/config/ stays public.
BILLING_ENABLED = os.environ.get("BILLING_ENABLED", "true").lower() == "true"
# ADR 020 — Organization / Membership HTTP surfaces under /api/v1/orgs/*
ORGANIZATIONS_ENABLED = (
    os.environ.get("ORGANIZATIONS_ENABLED", "true").lower() == "true"
)
# Pending org invite lifetime (ADR 020); accept is single-use regardless.
ORGANIZATION_INVITE_TTL_SECONDS = int(
    os.environ.get("ORGANIZATION_INVITE_TTL_SECONDS", str(7 * 24 * 3600))
)
SUBSCRIPTIONS_ENABLED = (
    os.environ.get("SUBSCRIPTIONS_ENABLED", "false").lower() == "true"
)
VOUCHERS_ENABLED = os.environ.get("VOUCHERS_ENABLED", "false").lower() == "true"
# eSIM Auto Top-up v1 (design lock) — master + rollout; spend still via TopupService.
AUTO_TOPUP_ENABLED = os.environ.get("AUTO_TOPUP_ENABLED", "false").lower() == "true"
AUTO_TOPUP_COOLDOWN_SECONDS = int(os.environ.get("AUTO_TOPUP_COOLDOWN_SECONDS", "900"))
AUTO_TOPUP_USAGE_MAX_AGE_SECONDS = int(
    os.environ.get("AUTO_TOPUP_USAGE_MAX_AGE_SECONDS", "600")
)
AUTO_TOPUP_MINIMUM_AGE_SECONDS = int(
    os.environ.get("AUTO_TOPUP_MINIMUM_AGE_SECONDS", "600")
)
AUTO_TOPUP_ROLLOUT_MODE = os.environ.get("AUTO_TOPUP_ROLLOUT_MODE", "off").lower()
AUTO_TOPUP_ALLOWLIST_ACCOUNT_IDS = [
    item.strip()
    for item in os.environ.get("AUTO_TOPUP_ALLOWLIST_ACCOUNT_IDS", "").split(",")
    if item.strip()
]
AUTO_TOPUP_ROLLOUT_PERCENT = int(os.environ.get("AUTO_TOPUP_ROLLOUT_PERCENT", "0"))
# ADR 019 — when false, PricingService always returns retail (list == customer).
PRICING_PROFILES_ENABLED = (
    os.environ.get("PRICING_PROFILES_ENABLED", "false").lower() == "true"
)
WALLETCONNECT_ENABLED = (
    os.environ.get("WALLETCONNECT_ENABLED", "false").lower() == "true"
)

# Cloudflare Turnstile (human verification on auth POSTs). Default off for local.
TURNSTILE_ENABLED = os.environ.get("TURNSTILE_ENABLED", "false").lower() == "true"
TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "")
TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "")
TURNSTILE_VERIFY_TIMEOUT = float(os.environ.get("TURNSTILE_VERIFY_TIMEOUT", "2.5"))
TURNSTILE_VERIFY_URL = os.environ.get(
    "TURNSTILE_VERIFY_URL",
    "https://challenges.cloudflare.com/turnstile/v0/siteverify",
)
TURNSTILE_TOKEN_SEEN_TTL = int(os.environ.get("TURNSTILE_TOKEN_SEEN_TTL", "180"))
TURNSTILE_BYPASS_SECRET = os.environ.get("TURNSTILE_BYPASS_SECRET", "")

# Google OAuth GIS ID token (ADR 015). Default off for local/dark deploy.
GOOGLE_OAUTH_ENABLED = os.environ.get("GOOGLE_OAUTH_ENABLED", "false").lower() == "true"
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_VERIFY_TIMEOUT = float(
    os.environ.get("GOOGLE_OAUTH_VERIFY_TIMEOUT", "2.5")
)
GOOGLE_OAUTH_CLOCK_SKEW_SECONDS = int(
    os.environ.get("GOOGLE_OAUTH_CLOCK_SKEW_SECONDS", "60")
)

# Platform Wallet Infrastructure (ADR 017 / RFC 004). BIP39 mnemonic for HD
# receive addresses; empty until allocate — never commit a real seed.
WALLET_HD_MNEMONIC = os.environ.get("WALLET_HD_MNEMONIC", "")

# Wallet Product Activation flags (ADR 018). Default off — shared ADR 010
# path remains primary until Phase 3 cutover GO.
WALLET_ADDRESS_ENABLED = (
    os.environ.get("WALLET_ADDRESS_ENABLED", "false").lower() == "true"
)
OBSERVATION_ENABLED = os.environ.get("OBSERVATION_ENABLED", "false").lower() == "true"
CREDIT_CONVERSION_V2 = os.environ.get("CREDIT_CONVERSION_V2", "false").lower() == "true"
SHADOW_MODE = os.environ.get("SHADOW_MODE", "false").lower() == "true"
# Explicit Account UUID allowlist for Phase 2 Limited Traffic (comma-separated).
# Empty = instant rollback to legacy ADR 010 for all accounts (no deploy).
WALLET_CUTOVER_COHORT_ACCOUNT_IDS = os.environ.get(
    "WALLET_CUTOVER_COHORT_ACCOUNT_IDS", ""
)

# Polygon USDT deposits (ADR-010). Defaults match mainnet; set wallet in env.
POLYGON_RPC_URL = os.environ.get("POLYGON_RPC_URL", "")
POLYGON_USDT_CONTRACT = os.environ.get(
    "POLYGON_USDT_CONTRACT",
    "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
)
POLYGON_PLATFORM_WALLET = os.environ.get("POLYGON_PLATFORM_WALLET", "")
POLYGON_CHAIN_ID = int(os.environ.get("POLYGON_CHAIN_ID", "137"))
POLYGON_MIN_CONFIRMATIONS = int(os.environ.get("POLYGON_MIN_CONFIRMATIONS", "20"))
POLYGON_USDT_DECIMALS = int(os.environ.get("POLYGON_USDT_DECIMALS", "6"))
POLYGON_RPC_TIMEOUT = float(os.environ.get("POLYGON_RPC_TIMEOUT", "10"))
POLYGON_RPC_RETRIES = int(os.environ.get("POLYGON_RPC_RETRIES", "3"))
POLYGON_RPC_BACKOFF_BASE = float(os.environ.get("POLYGON_RPC_BACKOFF_BASE", "0.5"))

FRONTEND_BASE_URL = os.environ.get("FRONTEND_BASE_URL", "http://localhost:3000").rstrip(
    "/"
)

EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "true").lower() == "true"
EMAIL_USE_SSL = os.environ.get("EMAIL_USE_SSL", "false").lower() == "true"
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@roamkit.net")

if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Default Django password-reset timeout (activate uses a dedicated generator).
PASSWORD_RESET_TIMEOUT = int(os.environ.get("PASSWORD_RESET_TIMEOUT", str(60 * 60)))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}

# Release metadata — consumed by GET /version and ops health (ADR 013).
ROAMKIT_GIT_SHA = os.environ.get("ROAMKIT_GIT_SHA", "")
ROAMKIT_BUILD_DATE = os.environ.get("ROAMKIT_BUILD_DATE", "")
ROAMKIT_IMAGE_TAG = os.environ.get("ROAMKIT_IMAGE_TAG", "")
ROAMKIT_ENVIRONMENT = os.environ.get("ROAMKIT_ENVIRONMENT", "")
ROAMKIT_RELEASE = os.environ.get("ROAMKIT_RELEASE", "")
ROAMKIT_DEPLOYMENT_ID = os.environ.get("ROAMKIT_DEPLOYMENT_ID", "")
