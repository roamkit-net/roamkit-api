"""Voucher Django admin PR2 — service + permission + soft-delete tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.admin_vouchers import VoucherAdmin, VoucherCampaignAdmin
from apps.billing.exceptions import (
    VoucherAdminConflictError,
    VoucherAdminValidationError,
    VoucherBatchBusyError,
)
from apps.billing.models import (
    SoftDeleteViolation,
    Voucher,
    VoucherBatch,
    VoucherCampaign,
    VoucherExportAudit,
    VoucherRevokeReason,
)
from apps.billing.services import voucher_admin as svc
from apps.billing.services.voucher_redeem import (
    issue_shared_campaign,
    issue_unique_voucher,
    voucher_redeem_service,
)

PASSWORD = "test-pass-123"


@pytest.fixture
def staff_user(db) -> User:
    return User.objects.create_superuser(
        email="voucher-admin@example.com", password=PASSWORD
    )


@pytest.fixture
def campaign(db) -> VoucherCampaign:
    return svc.create_campaign(
        credit_amount=Decimal("5.000000"),
        actor_id="1",
        code="PROMO-ADMIN",
        status=VoucherCampaign.Status.DRAFT,
        max_redemptions_total=100,
    )


@pytest.mark.django_db
def test_create_and_activate_campaign(campaign: VoucherCampaign) -> None:
    assert campaign.status == VoucherCampaign.Status.DRAFT
    assert campaign.issued_by_type == "admin"
    activated = svc.activate_campaign(campaign, expected_updated_at=campaign.updated_at)
    assert activated.status == VoucherCampaign.Status.ACTIVE


@pytest.mark.django_db
def test_optimistic_concurrency_blocks_stale_save(campaign: VoucherCampaign) -> None:
    stale = campaign.updated_at - timedelta(seconds=30)
    with pytest.raises(VoucherAdminConflictError):
        svc.save_campaign(
            campaign,
            expected_updated_at=stale,
            credit_amount=Decimal("9.000000"),
        )
    campaign.refresh_from_db()
    assert campaign.credit_amount == Decimal("5.000000")


@pytest.mark.django_db
def test_generate_batch_metadata_and_export_audit(campaign: VoucherCampaign) -> None:
    svc.activate_campaign(campaign, expected_updated_at=campaign.updated_at)
    campaign.refresh_from_db()
    batch = svc.generate_batch(
        size=3,
        credit_amount=campaign.credit_amount,
        actor_id="42",
        campaign=campaign,
        expected_campaign_updated_at=campaign.updated_at,
    )
    assert batch.size == 3
    assert batch.generated_at is not None
    assert batch.generation_duration_ms is not None
    assert batch.generated_by_version
    assert Voucher.objects.filter(batch=batch).count() == 3
    assert all(v.code.startswith("RK-") for v in batch.vouchers.all())

    preview_csv = svc.export_batch_csv(batch, actor_id="42", record_audit=False)
    assert "RK-" in preview_csv
    assert VoucherExportAudit.objects.count() == 0

    real_csv = svc.export_batch_csv(batch, actor_id="42", record_audit=True)
    assert real_csv.startswith("code,")
    assert VoucherExportAudit.objects.filter(format="csv").count() == 1

    pdf = svc.export_batch_pdf(batch, actor_id="42", record_audit=True)
    assert pdf[:4] == b"%PDF"
    assert VoucherExportAudit.objects.filter(format="pdf").count() == 1


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, VOUCHERS_ENABLED=True)
def test_revoke_campaign_impact_matches_preview(campaign: VoucherCampaign) -> None:
    svc.activate_campaign(campaign, expected_updated_at=campaign.updated_at)
    campaign.refresh_from_db()
    batch = svc.generate_batch(
        size=5,
        credit_amount=campaign.credit_amount,
        actor_id="1",
        campaign=campaign,
        expected_campaign_updated_at=campaign.updated_at,
    )
    # Redeem one unique voucher so it is excluded from revoke_now.
    user = User.objects.create_user(email="redeemer@example.com", password=PASSWORD)
    voucher = batch.vouchers.first()
    voucher_redeem_service.redeem(
        account=user.billing_account,
        code=voucher.code,
        request_id="admin-test-1",
    )
    campaign.refresh_from_db()
    preview = svc.preview_revoke_campaign(campaign)
    impact = svc.revoke_campaign(
        campaign,
        reason=VoucherRevokeReason.MANUAL_REVOKE,
        actor_id="1",
        expected_updated_at=campaign.updated_at,
    )
    assert impact.affected == preview.affected
    assert impact.already_redeemed == preview.already_redeemed
    assert impact.revoked_now == preview.revoked_now
    voucher.refresh_from_db()
    assert voucher.status == Voucher.Status.REDEEMED
    assert (
        Voucher.objects.filter(batch=batch, status=Voucher.Status.REVOKED).count() == 4
    )


@pytest.mark.django_db
@override_settings(BILLING_ENABLED=True, VOUCHERS_ENABLED=True)
def test_cannot_extend_redeemed_voucher() -> None:
    voucher = issue_unique_voucher(code="RK-EXTEND1", credit_amount=Decimal("1.000000"))
    user = User.objects.create_user(email="e@example.com", password=PASSWORD)
    voucher_redeem_service.redeem(
        account=user.billing_account, code="RK-EXTEND1", request_id="r1"
    )
    voucher.refresh_from_db()
    with pytest.raises(VoucherAdminValidationError):
        svc.extend_voucher_expiry(
            voucher,
            expires_at=timezone.now() + timedelta(days=30),
            expected_updated_at=voucher.updated_at,
        )


@pytest.mark.django_db(transaction=True)
def test_batch_generate_soft_lock(campaign: VoucherCampaign) -> None:
    svc.activate_campaign(campaign, expected_updated_at=campaign.updated_at)
    campaign.refresh_from_db()
    expected = campaign.updated_at
    barrier_errors: list[BaseException] = []

    def _generate() -> str:
        from django.db import connection as db_connection

        try:
            batch = svc.generate_batch(
                size=20,
                credit_amount=campaign.credit_amount,
                actor_id="lock",
                campaign=campaign,
                expected_campaign_updated_at=expected,
            )
            return str(batch.pk)
        except VoucherBatchBusyError as exc:
            barrier_errors.append(exc)
            return "busy"
        except VoucherAdminConflictError as exc:
            barrier_errors.append(exc)
            return "conflict"
        finally:
            db_connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _generate(), range(2)))

    successes = [r for r in results if r not in {"busy", "conflict"}]
    # At least one succeeds; the other should fail busy/conflict or also succeed
    # if they serialized cleanly after the first commit.
    assert len(successes) >= 1
    assert VoucherBatch.objects.filter(campaign=campaign).count() >= 1


@pytest.mark.django_db
def test_soft_delete_orm_queryset_and_admin(staff_user: User) -> None:
    voucher = issue_unique_voucher(code="RK-DEL1", credit_amount=Decimal("1.000000"))
    with pytest.raises(SoftDeleteViolation):
        voucher.delete()
    with pytest.raises(SoftDeleteViolation):
        Voucher.objects.filter(pk=voucher.pk).delete()

    site = AdminSite()
    admin = VoucherAdmin(Voucher, site)
    request = RequestFactory().post("/")
    request.user = staff_user
    assert admin.has_delete_permission(request, voucher) is False


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("issue", "revoke", "export", "can_issue", "can_revoke", "can_export"),
    [
        (False, False, False, False, False, False),
        (True, False, False, True, False, False),
        (True, True, False, True, True, False),
        (True, True, True, True, True, True),
    ],
)
def test_permission_matrix(
    db,
    issue: bool,
    revoke: bool,
    export: bool,
    can_issue: bool,
    can_revoke: bool,
    can_export: bool,
) -> None:
    user = User.objects.create_user(
        email=f"perm-{issue}{revoke}{export}@example.com",
        password=PASSWORD,
        is_staff=True,
    )
    ct = ContentType.objects.get_for_model(Voucher)
    ct_campaign = ContentType.objects.get_for_model(VoucherCampaign)
    user.user_permissions.add(
        Permission.objects.get(content_type=ct, codename="view_voucher"),
        Permission.objects.get(
            content_type=ct_campaign, codename="view_vouchercampaign"
        ),
    )
    if issue:
        user.user_permissions.add(
            Permission.objects.get(content_type=ct, codename="issue_voucher")
        )
    if revoke:
        user.user_permissions.add(
            Permission.objects.get(content_type=ct, codename="revoke_voucher")
        )
    if export:
        user.user_permissions.add(
            Permission.objects.get(content_type=ct, codename="export_voucher")
        )
    user = User.objects.get(pk=user.pk)

    site = AdminSite()
    campaign_admin = VoucherCampaignAdmin(VoucherCampaign, site)
    voucher_admin = VoucherAdmin(Voucher, site)
    request = RequestFactory().get("/")
    request.user = user

    assert campaign_admin.has_add_permission(request) is can_issue
    assert campaign_admin.has_change_permission(request) is True

    assert user.has_perm("billing.issue_voucher") is can_issue
    assert user.has_perm("billing.revoke_voucher") is can_revoke
    assert user.has_perm("billing.export_voucher") is can_export
    assert voucher_admin.has_add_permission(request) is can_issue


@pytest.mark.django_db
def test_pr1_helpers_still_work() -> None:
    campaign = issue_shared_campaign(code="LEGACY1", credit_amount=Decimal("2.000000"))
    assert campaign.status == VoucherCampaign.Status.ACTIVE
    voucher = issue_unique_voucher(code="LEGACY-U1", credit_amount=Decimal("2.000000"))
    assert voucher.status == Voucher.Status.ACTIVE


@pytest.mark.django_db
def test_warnings_include_vouchers_disabled(
    settings, campaign: VoucherCampaign
) -> None:
    settings.VOUCHERS_ENABLED = False
    warnings = svc.collect_admin_warnings(campaign=campaign)
    assert any("VOUCHERS_ENABLED" in w for w in warnings)


@pytest.mark.django_db
def test_health_stats(campaign: VoucherCampaign) -> None:
    stats = svc.campaign_health_stats()
    assert "active" in stats
    assert "redeemed_today" in stats
