"""eSIM models."""

import uuid

from django.conf import settings
from django.db import models
from django.db.models.functions import Trim


class ActivationPolicy(models.TextChoices):
    """When a data package starts counting (Airalo operator.activation_policy)."""

    FIRST_USAGE = "first_usage", "First usage"
    INSTALLATION = "installation", "Installation"
    UNKNOWN = "unknown", "Unknown"


class Esim(models.Model):
    """A provisioned eSIM.

    Inventory owner is ``account`` (ADR 020). ``user`` is retained for
    dual-read during cutover (personal Account's user). ``assigned_user`` is
    presentation/ops only — never financial or inventory owner.
    """

    class Status(models.TextChoices):
        PURCHASED = "purchased", "Purchased"
        INSTALLATION_STARTED = "installation_started", "Installation started"
        INSTALLED = "installed", "Installed"
        ACTIVATED = "activated", "Activated"
        IN_USE = "in_use", "In use"
        EXHAUSTED = "exhausted", "Exhausted"
        EXPIRED = "expired", "Expired"
        UNKNOWN = "unknown", "Unknown"

    account = models.ForeignKey(
        "billing.Account",
        on_delete=models.CASCADE,
        related_name="esims",
        help_text="Inventory owner (personal or organization Account).",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="esims",
        help_text=(
            "Legacy dual-read owner link (personal Account user). "
            "Do not use as inventory SoT — prefer ``account``."
        ),
    )
    assigned_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_esims",
        help_text=(
            "Optional assignee (who uses the SIM). Not inventory or "
            "financial owner — never used for authz or spend."
        ),
    )
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.CASCADE,
        related_name="esims",
    )
    iccid = models.CharField(max_length=32, unique=True, db_index=True)
    lpa = models.CharField(max_length=255, blank=True)
    matching_id = models.CharField(max_length=128, blank=True)
    qrcode = models.TextField(blank=True)
    qrcode_url = models.URLField(max_length=512, blank=True)
    direct_apple_installation_url = models.URLField(max_length=1024, blank=True)
    manual_installation = models.TextField(blank=True)
    qrcode_installation = models.TextField(blank=True)
    installation_guide_url = models.URLField(max_length=512, blank=True)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.PURCHASED,
        db_index=True,
    )
    # Purchase-time snapshot — never re-read from live Package after fulfill.
    activation_policy = models.CharField(
        max_length=32,
        choices=ActivationPolicy.choices,
        default=ActivationPolicy.UNKNOWN,
    )
    setup_version = models.CharField(max_length=32, blank=True, default="")
    setup_resume_step = models.PositiveSmallIntegerField(null=True, blank=True)
    setup_completed_at = models.DateTimeField(null=True, blank=True)
    setup_skipped_at = models.DateTimeField(null=True, blank=True)
    # Usage cache — populated by UsageService on usage fetch.
    usage_remaining_mb = models.IntegerField(null=True, blank=True)
    usage_total_mb = models.IntegerField(null=True, blank=True)
    usage_status = models.CharField(max_length=64, blank=True)
    usage_is_unlimited = models.BooleanField(null=True, blank=True)
    usage_expired_at = models.DateTimeField(null=True, blank=True)
    usage_synced_at = models.DateTimeField(null=True, blank=True)
    # User-local metadata — never synchronized to Airalo (or other providers).
    # Presentation-only cluster: note, archived_at; reserved later: favorite_at,
    # pinned_at, hidden_at. Never gate lifecycle/billing/provider work on these.
    note = models.CharField(max_length=255, blank=True, default="")
    archived_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "User-local visibility only. Must never affect lifecycle, top-up, "
            "billing, auto-topup, provider sync, usage refresh, or events."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["account", "status"]),
            models.Index(fields=["account", "archived_at"]),
            models.Index(fields=["user", "status"]),
            models.Index(fields=["user", "archived_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                Trim("matching_id"),
                condition=~models.Q(matching_id=""),
                name="esims_esim_matching_id_trimmed_nonempty_uniq",
            ),
        ]

    def save(self, *args, **kwargs) -> None:
        self.matching_id = (self.matching_id or "").strip()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"eSIM {self.iccid}"


class EsimLifecycleEvent(models.Model):
    """Append-only install / lifecycle telemetry (ADR 014)."""

    class Source(models.TextChoices):
        CLIENT = "client", "Client"
        SYSTEM = "system", "System"
        PROVIDER = "provider", "Provider"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    esim = models.ForeignKey(
        Esim,
        on_delete=models.CASCADE,
        related_name="lifecycle_events",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="esim_lifecycle_events",
    )
    event_type = models.CharField(max_length=64, db_index=True)
    source = models.CharField(
        max_length=16,
        choices=Source.choices,
        default=Source.CLIENT,
    )
    schema_version = models.PositiveSmallIntegerField(default=1)
    idempotency_key = models.CharField(max_length=128)
    setup_session_id = models.UUIDField(null=True, blank=True, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    user_agent = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["esim", "idempotency_key"],
                name="esims_lifecycle_event_esim_idem_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["esim", "created_at"]),
            models.Index(fields=["event_type", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.event_type} ({self.esim_id})"


class Topup(models.Model):
    """A prepaid credit spend that applies a top-up package to an eSIM."""

    class Status(models.TextChoices):
        FULFILLING = "fulfilling", "Fulfilling"
        FULFILLED = "fulfilled", "Fulfilled"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        "billing.Account",
        on_delete=models.CASCADE,
        related_name="topups",
    )
    esim = models.ForeignKey(
        Esim,
        on_delete=models.CASCADE,
        related_name="topups",
    )
    package_external_id = models.CharField(max_length=64, db_index=True)
    amount = models.DecimalField(max_digits=20, decimal_places=6)
    list_price_usd = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Provider list price at purchase (ADR 019).",
    )
    discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
    )
    pricing_reason = models.CharField(max_length=32, blank=True, default="")
    floor_reason = models.CharField(max_length=32, blank=True, default="")
    pricing_profile_id = models.UUIDField(null=True, blank=True)
    pricing_profile_version = models.PositiveIntegerField(null=True, blank=True)
    pricing_profile_slug = models.CharField(max_length=128, blank=True, default="")
    pricing_profile_name = models.CharField(max_length=128, blank=True, default="")
    pricing_context_hash = models.CharField(max_length=64, blank=True, default="")
    snapshot_schema_version = models.PositiveSmallIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.FULFILLING,
        db_index=True,
    )
    external_order_id = models.CharField(max_length=64, blank=True, db_index=True)
    idempotency_key = models.CharField(
        max_length=128,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "top-up"
        verbose_name_plural = "top-ups"
        indexes = [
            models.Index(fields=["account", "status"]),
            models.Index(fields=["esim", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="esims_topup_amount_gt_0",
            ),
        ]

    def __str__(self) -> str:
        return f"Topup {self.pk} ({self.status})"


class EsimAutoTopupPolicy(models.Model):
    """User policy to auto-purchase Available top-ups from usage/expiry triggers.

    eSIM-domain only — not ``billing.Subscription`` (calendar renew). Spend still
    goes through ``TopupService.purchase`` / ``CreditService`` (design lock v2).
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        BLOCKED = "blocked", "Blocked"
        DISABLED = "disabled", "Disabled"

    class Reason(models.TextChoices):
        INSUFFICIENT_FUNDS = "insufficient_funds", "Insufficient funds"
        PACKAGE_UNAVAILABLE = "package_unavailable", "Package unavailable"
        USAGE_UNKNOWN = "usage_unknown", "Usage unknown"
        PROVIDER_ERROR = "provider_error", "Provider error"
        MANUAL_PAUSE = "manual_pause", "Manual pause"
        COUNT_EXHAUSTED = "count_exhausted", "Count exhausted"
        SCHEDULE_ENDED = "schedule_ended", "Schedule ended"

    class UsageMode(models.TextChoices):
        DISABLED = "disabled", "Disabled"
        THRESHOLD = "threshold", "Threshold"
        ZERO = "zero", "Zero"

    class RenewMode(models.TextChoices):
        UNTIL_FUNDS = "until_funds", "Until funds"
        FIXED_COUNT = "fixed_count", "Fixed count"

    # Fire-reason / idempotency strings (and best-effort event snapshots).
    LEGACY_TRIGGER_EXPIRY = "expiry"
    LEGACY_TRIGGER_USAGE_THRESHOLD = "usage_threshold"
    LEGACY_TRIGGER_USAGE_ZERO = "usage_zero"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        "billing.Account",
        on_delete=models.CASCADE,
        related_name="auto_topup_policies",
    )
    esim = models.ForeignKey(
        Esim,
        on_delete=models.CASCADE,
        related_name="auto_topup_policies",
    )
    package_id = models.CharField(max_length=64, db_index=True)
    enabled = models.BooleanField(default=True, db_index=True)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )
    reason = models.CharField(
        max_length=32,
        choices=Reason.choices,
        blank=True,
        default="",
    )
    expiry_enabled = models.BooleanField(default=False)
    usage_mode = models.CharField(
        max_length=32,
        choices=UsageMode.choices,
        default=UsageMode.DISABLED,
    )
    threshold_mb = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Required when usage_mode is threshold.",
    )
    renew_mode = models.CharField(
        max_length=32,
        choices=RenewMode.choices,
    )
    remaining_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Required when renew_mode is fixed_count.",
    )
    active_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Optional UTC exclusive end bound for policy lifetime (v3). "
            "Null means no schedule limit."
        ),
    )
    cooldown_until = models.DateTimeField(null=True, blank=True)
    last_triggered_at = models.DateTimeField(null=True, blank=True)
    last_topup = models.ForeignKey(
        Topup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    last_idempotency_key = models.CharField(max_length=128, blank=True, default="")
    version = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "eSIM auto top-up policy"
        verbose_name_plural = "eSIM auto top-up policies"
        indexes = [
            models.Index(fields=["status", "enabled"]),
            models.Index(fields=["account", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["esim"],
                name="esims_auto_topup_policy_esim_uniq",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(usage_mode="threshold")
                    | models.Q(threshold_mb__isnull=False)
                ),
                name="esims_auto_topup_threshold_required",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(renew_mode="fixed_count")
                    | models.Q(remaining_count__isnull=False)
                ),
                name="esims_auto_topup_count_required",
            ),
        ]

    def __str__(self) -> str:
        return f"AutoTopupPolicy {self.pk} ({self.status})"

    @classmethod
    def fields_from_legacy_trigger(cls, trigger_mode: str) -> tuple[bool, str]:
        """Map v1 ``trigger_mode`` → ``(expiry_enabled, usage_mode)``."""
        if trigger_mode == cls.LEGACY_TRIGGER_EXPIRY:
            return True, cls.UsageMode.DISABLED
        if trigger_mode == cls.LEGACY_TRIGGER_USAGE_THRESHOLD:
            return False, cls.UsageMode.THRESHOLD
        if trigger_mode == cls.LEGACY_TRIGGER_USAGE_ZERO:
            return False, cls.UsageMode.ZERO
        raise ValueError(f"Unknown legacy trigger_mode: {trigger_mode!r}")

    def apply_legacy_trigger_mode(
        self, trigger_mode: str, *, threshold_mb: int | None
    ) -> None:
        expiry_enabled, usage_mode = self.fields_from_legacy_trigger(trigger_mode)
        self.expiry_enabled = expiry_enabled
        self.usage_mode = usage_mode
        if usage_mode == self.UsageMode.THRESHOLD:
            self.threshold_mb = threshold_mb
        else:
            self.threshold_mb = None

    def legacy_trigger_mode(self) -> str:
        """Best-effort single fire-reason label for domain events.

        Combo policies (expiry + usage) are not representable as one v1 mode;
        prefer expiry when both legs are configured.
        """
        if self.expiry_enabled and self.usage_mode == self.UsageMode.DISABLED:
            return self.LEGACY_TRIGGER_EXPIRY
        if not self.expiry_enabled and self.usage_mode == self.UsageMode.THRESHOLD:
            return self.LEGACY_TRIGGER_USAGE_THRESHOLD
        if not self.expiry_enabled and self.usage_mode == self.UsageMode.ZERO:
            return self.LEGACY_TRIGGER_USAGE_ZERO
        if self.expiry_enabled:
            return self.LEGACY_TRIGGER_EXPIRY
        if self.usage_mode == self.UsageMode.THRESHOLD:
            return self.LEGACY_TRIGGER_USAGE_THRESHOLD
        if self.usage_mode == self.UsageMode.ZERO:
            return self.LEGACY_TRIGGER_USAGE_ZERO
        return self.LEGACY_TRIGGER_EXPIRY
