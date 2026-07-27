"""Voucher admin issuance / revoke / export (ADR 011 PR2) — no CreditService."""

from __future__ import annotations

import csv
import io
import logging
import secrets
import time
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.db import OperationalError, connection, transaction
from django.utils import timezone

from apps.billing.exceptions import (
    VoucherAdminConflictError,
    VoucherAdminValidationError,
    VoucherBatchBusyError,
)
from apps.billing.models import (
    RedemptionMode,
    RewardType,
    Voucher,
    VoucherBatch,
    VoucherCampaign,
    VoucherExportAudit,
    VoucherExportFormat,
    VoucherIssuerType,
    VoucherRevokeReason,
    VoucherType,
)
from apps.billing.voucher_codes import assert_code_available

logger = logging.getLogger(__name__)

VOUCHER_CODE_PREFIX = "RK-"
_CODE_COLLISION_RETRIES = 8
_REVOCABLE_VOUCHER_STATUSES = frozenset({Voucher.Status.CREATED, Voucher.Status.ACTIVE})


@dataclass(frozen=True)
class RevokeImpact:
    affected: int
    already_redeemed: int
    revoked_now: int


@dataclass(frozen=True)
class BatchPreview:
    size: int
    reward_type: str
    credit_amount: Decimal
    expires_at: datetime | None
    code_prefix: str
    irreversible_warning: str


def _release_version() -> str:
    sha = (getattr(settings, "ROAMKIT_GIT_SHA", "") or "").strip()
    tag = (getattr(settings, "ROAMKIT_IMAGE_TAG", "") or "").strip()
    if sha:
        return sha[:40]
    if tag:
        return tag[:64]
    return "dev"


def _actor_id(actor_id: str | int | uuid.UUID | None) -> str:
    if actor_id is None:
        return ""
    return str(actor_id)


def _require_revoke_reason(reason: str) -> str:
    valid = {c.value for c in VoucherRevokeReason}
    if reason not in valid:
        raise VoucherAdminValidationError("revoke_reason is required")
    return reason


def _assert_updated_at(
    instance: VoucherCampaign | Voucher | VoucherBatch,
    expected_updated_at: datetime | None,
) -> None:
    if expected_updated_at is None:
        return
    current = instance.updated_at
    if current is None:
        return
    # Compare at microsecond precision; admin form may round.
    if abs((current - expected_updated_at).total_seconds()) > 0.001:
        raise VoucherAdminConflictError(
            "Reload and retry — the record was changed by another operator."
        )


def _advisory_lock_campaign(campaign_id: uuid.UUID) -> None:
    """Transaction-scoped advisory lock; fail fast if another generate holds it."""
    key = (
        int.from_bytes(uuid.UUID(str(campaign_id)).bytes[:8], "big")
        & 0x7FFFFFFFFFFFFFFF
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [key])
        got = cursor.fetchone()[0]
    if not got:
        raise VoucherBatchBusyError("Batch generation already in progress.")


def generate_unique_code() -> str:
    """Generate ``RK-…`` code that passes ``assert_code_available``."""
    for _ in range(_CODE_COLLISION_RETRIES):
        candidate = f"{VOUCHER_CODE_PREFIX}{secrets.token_hex(8).upper()}"
        try:
            return assert_code_available(candidate)
        except ValueError:
            continue
    raise VoucherAdminValidationError("Could not allocate a unique voucher code")


def create_campaign(
    *,
    credit_amount: Decimal,
    actor_id: str | int | uuid.UUID | None,
    code: str | None = None,
    redemption_mode: str = RedemptionMode.SHARED,
    voucher_type: str = VoucherType.PROMO,
    reward_type: str = RewardType.FIXED_CREDIT,
    max_redemptions_total: int | None = None,
    max_redemptions_per_account: int | None = 1,
    starts_at: datetime | None = None,
    expires_at: datetime | None = None,
    status: str = VoucherCampaign.Status.DRAFT,
) -> VoucherCampaign:
    if reward_type != RewardType.FIXED_CREDIT:
        raise VoucherAdminValidationError("Only fixed_credit is supported in v1")
    if credit_amount <= 0:
        raise VoucherAdminValidationError("credit_amount must be positive")
    normalized = assert_code_available(code) if code else None
    return VoucherCampaign.objects.create(
        code=normalized,
        redemption_mode=redemption_mode,
        voucher_type=voucher_type,
        reward_type=reward_type,
        credit_amount=credit_amount,
        max_redemptions_total=max_redemptions_total,
        max_redemptions_per_account=max_redemptions_per_account,
        starts_at=starts_at,
        expires_at=expires_at,
        status=status,
        issued_by_type=VoucherIssuerType.ADMIN,
        issued_by_id=_actor_id(actor_id),
    )


@transaction.atomic
def save_campaign(
    campaign: VoucherCampaign,
    *,
    expected_updated_at: datetime | None,
    **fields,
) -> VoucherCampaign:
    locked = VoucherCampaign.objects.select_for_update().get(pk=campaign.pk)
    _assert_updated_at(locked, expected_updated_at)
    if locked.status == VoucherCampaign.Status.REVOKED:
        raise VoucherAdminValidationError("Cannot edit a revoked campaign")
    protected = {
        "id",
        "issued_by_type",
        "issued_by_id",
        "created_at",
        "revoked_at",
        "revoked_by_id",
    }
    for key, value in fields.items():
        if key in protected:
            continue
        if key == "code" and value:
            value = assert_code_available(value, exclude_campaign_id=locked.pk)
        setattr(locked, key, value)
    locked.save()
    return locked


@transaction.atomic
def activate_campaign(
    campaign: VoucherCampaign,
    *,
    expected_updated_at: datetime | None = None,
) -> VoucherCampaign:
    locked = VoucherCampaign.objects.select_for_update().get(pk=campaign.pk)
    _assert_updated_at(locked, expected_updated_at)
    if locked.status != VoucherCampaign.Status.DRAFT:
        raise VoucherAdminValidationError("Only DRAFT campaigns can be activated")
    locked.status = VoucherCampaign.Status.ACTIVE
    locked.save(update_fields=["status", "updated_at"])
    return locked


def activate_campaigns(campaigns: Iterable[VoucherCampaign]) -> int:
    activated = 0
    for campaign in campaigns:
        if campaign.status != VoucherCampaign.Status.DRAFT:
            continue
        activate_campaign(campaign, expected_updated_at=campaign.updated_at)
        activated += 1
    return activated


def _voucher_revoke_counts(qs) -> RevokeImpact:
    affected = qs.count()
    already_redeemed = qs.filter(status=Voucher.Status.REDEEMED).count()
    revoked_now = qs.filter(status__in=_REVOCABLE_VOUCHER_STATUSES).count()
    return RevokeImpact(
        affected=affected,
        already_redeemed=already_redeemed,
        revoked_now=revoked_now,
    )


def preview_revoke_campaign(campaign: VoucherCampaign) -> RevokeImpact:
    linked = Voucher.objects.filter(campaign=campaign)
    base = _voucher_revoke_counts(linked)
    campaign_revocable = campaign.status != VoucherCampaign.Status.REVOKED
    return RevokeImpact(
        affected=base.affected + 1,
        already_redeemed=base.already_redeemed,
        revoked_now=base.revoked_now + (1 if campaign_revocable else 0),
    )


def preview_revoke_batch(batch: VoucherBatch) -> RevokeImpact:
    return _voucher_revoke_counts(Voucher.objects.filter(batch=batch))


def preview_revoke_vouchers(vouchers: Iterable[Voucher]) -> RevokeImpact:
    ids = [v.pk for v in vouchers]
    return _voucher_revoke_counts(Voucher.objects.filter(pk__in=ids))


def _apply_voucher_revokes(
    qs,
    *,
    reason: str,
    actor_id: str,
    now: datetime,
) -> int:
    revocable = list(
        qs.filter(status__in=_REVOCABLE_VOUCHER_STATUSES).select_for_update()
    )
    for voucher in revocable:
        voucher.status = Voucher.Status.REVOKED
        voucher.revoke_reason = reason
        voucher.revoked_at = now
        voucher.revoked_by_id = actor_id
        voucher.save(
            update_fields=[
                "status",
                "revoke_reason",
                "revoked_at",
                "revoked_by_id",
                "updated_at",
            ]
        )
    return len(revocable)


@transaction.atomic
def revoke_campaign(
    campaign: VoucherCampaign,
    *,
    reason: str,
    actor_id: str | int | uuid.UUID | None,
    expected_updated_at: datetime | None = None,
) -> RevokeImpact:
    reason = _require_revoke_reason(reason)
    actor = _actor_id(actor_id)
    now = timezone.now()
    locked = VoucherCampaign.objects.select_for_update().get(pk=campaign.pk)
    _assert_updated_at(locked, expected_updated_at)
    if locked.status == VoucherCampaign.Status.REVOKED:
        raise VoucherAdminValidationError("Campaign is already revoked")
    preview = preview_revoke_campaign(locked)
    linked = Voucher.objects.filter(campaign=locked)
    revoked_now = _apply_voucher_revokes(linked, reason=reason, actor_id=actor, now=now)
    locked.status = VoucherCampaign.Status.REVOKED
    locked.revoke_reason = reason
    locked.revoked_at = now
    locked.revoked_by_id = actor
    locked.save(
        update_fields=[
            "status",
            "revoke_reason",
            "revoked_at",
            "revoked_by_id",
            "updated_at",
        ]
    )
    return RevokeImpact(
        affected=preview.affected,
        already_redeemed=preview.already_redeemed,
        revoked_now=revoked_now + 1,
    )


@transaction.atomic
def revoke_batch(
    batch: VoucherBatch,
    *,
    reason: str,
    actor_id: str | int | uuid.UUID | None,
    expected_updated_at: datetime | None = None,
) -> RevokeImpact:
    reason = _require_revoke_reason(reason)
    actor = _actor_id(actor_id)
    now = timezone.now()
    locked = VoucherBatch.objects.select_for_update().get(pk=batch.pk)
    _assert_updated_at(locked, expected_updated_at)
    if locked.status == VoucherBatch.Status.REVOKED:
        raise VoucherAdminValidationError("Batch is already revoked")
    preview = preview_revoke_batch(locked)
    revoked_now = _apply_voucher_revokes(
        Voucher.objects.filter(batch=locked),
        reason=reason,
        actor_id=actor,
        now=now,
    )
    locked.status = VoucherBatch.Status.REVOKED
    locked.save(update_fields=["status", "updated_at"])
    return RevokeImpact(
        affected=preview.affected,
        already_redeemed=preview.already_redeemed,
        revoked_now=revoked_now,
    )


@transaction.atomic
def revoke_vouchers(
    vouchers: Iterable[Voucher],
    *,
    reason: str,
    actor_id: str | int | uuid.UUID | None,
) -> RevokeImpact:
    reason = _require_revoke_reason(reason)
    actor = _actor_id(actor_id)
    now = timezone.now()
    ids = [v.pk for v in vouchers]
    qs = Voucher.objects.filter(pk__in=ids)
    preview = _voucher_revoke_counts(qs)
    revoked_now = _apply_voucher_revokes(qs, reason=reason, actor_id=actor, now=now)
    return RevokeImpact(
        affected=preview.affected,
        already_redeemed=preview.already_redeemed,
        revoked_now=revoked_now,
    )


@transaction.atomic
def extend_campaign_expiry(
    campaign: VoucherCampaign,
    *,
    expires_at: datetime,
    expected_updated_at: datetime | None = None,
) -> VoucherCampaign:
    locked = VoucherCampaign.objects.select_for_update().get(pk=campaign.pk)
    _assert_updated_at(locked, expected_updated_at)
    if locked.status in {
        VoucherCampaign.Status.REVOKED,
    }:
        raise VoucherAdminValidationError("Cannot extend a revoked campaign")
    locked.expires_at = expires_at
    if locked.status == VoucherCampaign.Status.EXPIRED:
        locked.status = VoucherCampaign.Status.ACTIVE
        locked.save(update_fields=["expires_at", "status", "updated_at"])
    else:
        locked.save(update_fields=["expires_at", "updated_at"])
    return locked


@transaction.atomic
def extend_voucher_expiry(
    voucher: Voucher,
    *,
    expires_at: datetime,
    expected_updated_at: datetime | None = None,
) -> Voucher:
    locked = Voucher.objects.select_for_update().get(pk=voucher.pk)
    _assert_updated_at(locked, expected_updated_at)
    if locked.status in {Voucher.Status.REDEEMED, Voucher.Status.REVOKED}:
        raise VoucherAdminValidationError(
            "Cannot extend expiry on redeemed or revoked vouchers"
        )
    locked.expires_at = expires_at
    if locked.status == Voucher.Status.EXPIRED:
        locked.status = Voucher.Status.ACTIVE
        locked.save(update_fields=["expires_at", "status", "updated_at"])
    else:
        locked.save(update_fields=["expires_at", "updated_at"])
    return locked


def preview_batch(
    *,
    size: int,
    credit_amount: Decimal,
    reward_type: str = RewardType.FIXED_CREDIT,
    expires_at: datetime | None = None,
) -> BatchPreview:
    if size < 1:
        raise VoucherAdminValidationError("size must be >= 1")
    if credit_amount <= 0:
        raise VoucherAdminValidationError("credit_amount must be positive")
    if reward_type != RewardType.FIXED_CREDIT:
        raise VoucherAdminValidationError("Only fixed_credit is supported in v1")
    return BatchPreview(
        size=size,
        reward_type=reward_type,
        credit_amount=credit_amount,
        expires_at=expires_at,
        code_prefix=VOUCHER_CODE_PREFIX,
        irreversible_warning=(
            "This operation cannot be undone. Generated codes can only be "
            "revoked, not deleted."
        ),
    )


@transaction.atomic
def generate_batch(
    *,
    size: int,
    credit_amount: Decimal,
    actor_id: str | int | uuid.UUID | None,
    campaign: VoucherCampaign | None = None,
    expected_campaign_updated_at: datetime | None = None,
    voucher_type: str = VoucherType.GIFT,
    reward_type: str = RewardType.FIXED_CREDIT,
    expires_at: datetime | None = None,
    activate: bool = True,
) -> VoucherBatch:
    preview_batch(
        size=size,
        credit_amount=credit_amount,
        reward_type=reward_type,
        expires_at=expires_at,
    )
    actor = _actor_id(actor_id)
    if campaign is not None:
        try:
            locked_campaign = VoucherCampaign.objects.select_for_update(
                nowait=True
            ).get(pk=campaign.pk)
        except OperationalError as exc:
            raise VoucherBatchBusyError(
                "Batch generation already in progress."
            ) from exc
        _assert_updated_at(locked_campaign, expected_campaign_updated_at)
        _advisory_lock_campaign(locked_campaign.pk)
        if locked_campaign.status == VoucherCampaign.Status.REVOKED:
            raise VoucherAdminValidationError(
                "Cannot generate a batch for a revoked campaign"
            )
        campaign = locked_campaign
        credit_amount = campaign.credit_amount
        voucher_type = campaign.voucher_type
        reward_type = campaign.reward_type
        if expires_at is None:
            expires_at = campaign.expires_at

    started = time.perf_counter()
    batch = VoucherBatch.objects.create(
        campaign=campaign,
        size=size,
        status=VoucherBatch.Status.ACTIVE if activate else VoucherBatch.Status.CREATED,
        issued_by_type=VoucherIssuerType.ADMIN,
        issued_by_id=actor,
    )
    voucher_status = Voucher.Status.ACTIVE if activate else Voucher.Status.CREATED
    vouchers: list[Voucher] = []
    for _ in range(size):
        vouchers.append(
            Voucher(
                code=generate_unique_code(),
                redemption_mode=RedemptionMode.UNIQUE,
                voucher_type=voucher_type,
                reward_type=reward_type,
                credit_amount=credit_amount,
                campaign=campaign,
                batch=batch,
                status=voucher_status,
                issued_by_type=VoucherIssuerType.ADMIN,
                issued_by_id=actor,
                expires_at=expires_at,
            )
        )
    Voucher.objects.bulk_create(vouchers)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    batch.generated_at = timezone.now()
    batch.generation_duration_ms = elapsed_ms
    batch.generated_by_version = _release_version()
    batch.save(
        update_fields=[
            "generated_at",
            "generation_duration_ms",
            "generated_by_version",
            "updated_at",
        ]
    )
    logger.info(
        "voucher_batch_generated batch_id=%s size=%s duration_ms=%s",
        batch.pk,
        size,
        elapsed_ms,
    )
    return batch


def iter_batch_export_rows(batch: VoucherBatch) -> Iterator[dict[str, str]]:
    """Iterator of CSV row dicts — ready for StreamingHttpResponse later."""
    qs = (
        Voucher.objects.filter(batch=batch)
        .order_by("created_at")
        .values_list("code", "credit_amount", "status", "expires_at")
    )
    for code, amount, status, expires_at in qs.iterator(chunk_size=500):
        yield {
            "code": code,
            "credit_amount": f"{amount:.6f}",
            "status": status,
            "expires_at": expires_at.isoformat() if expires_at else "",
        }


def build_batch_csv_text(batch: VoucherBatch) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=["code", "credit_amount", "status", "expires_at"],
    )
    writer.writeheader()
    for row in iter_batch_export_rows(batch):
        writer.writerow(row)
    return buffer.getvalue()


def build_batch_pdf_bytes(batch: VoucherBatch) -> bytes:
    """Minimal PDF (no ReportLab/Pillow) listing batch codes."""
    lines = [
        f"RoamKit voucher batch {batch.pk}",
        f"Size={batch.size} status={batch.status}",
        "",
    ]
    for row in iter_batch_export_rows(batch):
        lines.append(f"{row['code']}  {row['credit_amount']}  {row['status']}")
    return _minimal_pdf_bytes(lines)


def _minimal_pdf_bytes(lines: list[str]) -> bytes:
    """Build a one-page PDF 1.4 document with Helvetica text lines."""

    # Escape PDF string specials.
    def _esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    y = 800
    content_cmds = ["BT", "/F1 10 Tf", "50 800 Td"]
    first = True
    for line in lines:
        if not first:
            content_cmds.append("0 -12 Td")
        first = False
        content_cmds.append(f"({_esc(line[:110])}) Tj")
        y -= 12
        if y < 40:
            break
    content_cmds.append("ET")
    stream = "\n".join(content_cmds).encode("latin-1", errors="replace")

    objects: list[bytes] = []
    objects.append(b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n")
    objects.append(b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n")
    objects.append(
        b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>endobj\n"
    )
    objects.append(
        f"4 0 obj<< /Length {len(stream)} >>stream\n".encode()
        + stream
        + b"\nendstream\nendobj\n"
    )
    objects.append(
        b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n"
    )

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(out))
        out.extend(obj)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode())
    out.extend(
        f"trailer<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n".encode()
    )
    return bytes(out)


def build_vouchers_csv_text(vouchers: Iterable[Voucher]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "code",
            "credit_amount",
            "status",
            "expires_at",
            "campaign",
            "batch",
        ],
    )
    writer.writeheader()
    for voucher in vouchers:
        writer.writerow(
            {
                "code": voucher.code,
                "credit_amount": f"{voucher.credit_amount:.6f}",
                "status": voucher.status,
                "expires_at": (
                    voucher.expires_at.isoformat() if voucher.expires_at else ""
                ),
                "campaign": str(voucher.campaign_id or ""),
                "batch": str(voucher.batch_id or ""),
            }
        )
    return buffer.getvalue()


def _record_export(
    batch: VoucherBatch,
    *,
    fmt: str,
    actor_id: str | int | uuid.UUID | None,
) -> VoucherExportAudit:
    return VoucherExportAudit.objects.create(
        batch=batch,
        format=fmt,
        exported_by_id=_actor_id(actor_id),
        exported_at=timezone.now(),
    )


def export_batch_csv(
    batch: VoucherBatch,
    *,
    actor_id: str | int | uuid.UUID | None,
    record_audit: bool = True,
) -> str:
    text = build_batch_csv_text(batch)
    if record_audit:
        _record_export(batch, fmt=VoucherExportFormat.CSV, actor_id=actor_id)
    return text


def export_batch_pdf(
    batch: VoucherBatch,
    *,
    actor_id: str | int | uuid.UUID | None,
    record_audit: bool = True,
) -> bytes:
    payload = build_batch_pdf_bytes(batch)
    if record_audit:
        _record_export(batch, fmt=VoucherExportFormat.PDF, actor_id=actor_id)
    return payload


def campaign_health_stats() -> dict[str, int]:
    today = timezone.localdate()
    from apps.billing.models import VoucherRedemption

    return {
        "active": VoucherCampaign.objects.filter(
            status=VoucherCampaign.Status.ACTIVE
        ).count(),
        "expired": VoucherCampaign.objects.filter(
            status=VoucherCampaign.Status.EXPIRED
        ).count(),
        "revoked": VoucherCampaign.objects.filter(
            status=VoucherCampaign.Status.REVOKED
        ).count(),
        "redeemed_today": VoucherRedemption.objects.filter(
            redeemed_at__date=today
        ).count(),
    }


def collect_admin_warnings(
    *,
    campaign: VoucherCampaign | None = None,
    batch: VoucherBatch | None = None,
) -> list[str]:
    warnings: list[str] = []
    if not getattr(settings, "VOUCHERS_ENABLED", False):
        warnings.append("Redeem disabled (VOUCHERS_ENABLED=false).")
    now = timezone.now()
    if campaign is not None:
        if campaign.status == VoucherCampaign.Status.EXPIRED or (
            campaign.expires_at is not None and campaign.expires_at <= now
        ):
            warnings.append("Campaign expired.")
        if campaign.status == VoucherCampaign.Status.REVOKED:
            warnings.append("Campaign revoked.")
        if campaign.max_redemptions_total is not None:
            used = campaign.redemptions.count()
            if used >= campaign.max_redemptions_total:
                warnings.append(
                    "Vouchers remaining = 0 (campaign total limit reached)."
                )
    if batch is not None:
        if batch.status == VoucherBatch.Status.REVOKED:
            warnings.append("Batch revoked.")
        remaining = batch.vouchers.filter(
            status__in={Voucher.Status.CREATED, Voucher.Status.ACTIVE}
        ).count()
        if remaining == 0 and batch.size > 0:
            warnings.append("Vouchers remaining = 0.")
    return warnings
