"""InviteVisit creation, UTM, validator, and join/regenerate locking."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection, models, transaction
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import AppendOnlyViolation
from apps.billing.partner_channel import InviteVisit, PartnerInviteLink
from apps.billing.services.partner_invite import (
    canonical_invite_link,
    create_partner_channel,
    issue_join_signature,
    regenerate_invite_link,
)
from apps.billing.services.partner_invite_visit import (
    ATTRIBUTION_WINDOW,
    normalize_utm,
    normalize_utm_value,
    record_visit,
    validate_invite_visit,
)
from apps.billing.services.partner_pending import (
    SIGNATURE_MAX_AGE_SECONDS,
    unsign_partner_pending,
)
from apps.organizations.services.account_binding import create_organization

ENABLED = override_settings(
    PARTNER_CHANNEL_ENABLED=True,
    PARTNER_JOIN_BASE_URL="https://roamkit.net",
)


def _user(prefix: str) -> User:
    return User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4()}@example.com",
        password="secret123",
    )


def _channel():
    actor = _user("owner")
    org = create_organization(name=f"Org {uuid.uuid4()}", actor=actor)
    return create_partner_channel(organization=org), actor


@ENABLED
@pytest.mark.django_db
def test_active_token_records_one_visit_per_call() -> None:
    channel, _actor = _channel()
    token = canonical_invite_link(channel).token
    assert record_visit(token) is not None
    assert record_visit(token) is not None
    assert InviteVisit.objects.filter(invite_link__partner_channel=channel).count() == 2


@ENABLED
@pytest.mark.django_db
def test_invalid_or_inactive_token_records_nothing() -> None:
    channel, actor = _channel()
    assert record_visit("missing-token") is None
    assert InviteVisit.objects.count() == 0
    link = canonical_invite_link(channel)
    link.is_active = False
    link.save(update_fields=["is_active", "updated_at"])
    assert record_visit(link.token) is None
    assert InviteVisit.objects.count() == 0
    assert issue_join_signature("missing-token") is None
    regenerate_invite_link(channel, actor=actor)


@ENABLED
@pytest.mark.django_db
def test_utm_is_stored_truncated_and_first_value_only() -> None:
    channel, _actor = _channel()
    token = canonical_invite_link(channel).token
    long_value = "x" * 129
    recorded = record_visit(
        token,
        {
            "utm_source": ["tiktok", "instagram"],
            "utm_medium": "",
            "utm_campaign": long_value,
        },
    )
    assert recorded is not None
    visit, _signed = recorded
    assert visit.utm_source == "tiktok"
    assert visit.utm_medium == ""
    assert visit.utm_campaign == "x" * 128
    assert visit.utm_content == ""
    assert normalize_utm_value("  TikTok  ") == "  TikTok  "
    assert normalize_utm(None) == {
        "utm_source": "",
        "utm_medium": "",
        "utm_campaign": "",
        "utm_content": "",
    }


@ENABLED
@pytest.mark.django_db
def test_signed_payload_is_visit_id_and_issued_at_for_30_days() -> None:
    channel, _actor = _channel()
    signed = issue_join_signature(canonical_invite_link(channel).token)
    assert signed is not None
    data = unsign_partner_pending(signed)
    assert data is not None
    assert set(data) == {"visit_id", "issued_at"}
    assert InviteVisit.objects.filter(pk=data["visit_id"]).exists()
    assert SIGNATURE_MAX_AGE_SECONDS == 30 * 24 * 60 * 60
    assert unsign_partner_pending("not-a-signature") is None


@ENABLED
@pytest.mark.django_db
def test_validator_window_regeneration_and_inactive_link() -> None:
    channel, actor = _channel()
    token = canonical_invite_link(channel).token
    recorded = record_visit(token, {"utm_source": "whatsapp"})
    assert recorded is not None
    visit, _signed = recorded

    assert validate_invite_visit(visit.id) == visit
    assert validate_invite_visit(visit.id, check_attribution_window=False) == visit

    InviteVisit.objects.filter(pk=visit.pk).update(
        created_at=timezone.now() - ATTRIBUTION_WINDOW - timedelta(seconds=1)
    )
    visit.refresh_from_db()
    assert validate_invite_visit(visit.id) is None
    assert validate_invite_visit(visit.id, check_attribution_window=False) == visit

    InviteVisit.objects.filter(pk=visit.pk).update(created_at=timezone.now())
    old_token = token
    regenerate_invite_link(channel, actor=actor)
    visit.refresh_from_db()
    assert visit.created_at < visit.invite_link.regenerated_at
    assert validate_invite_visit(visit.id, check_attribution_window=False) is None
    assert InviteVisit.objects.filter(pk=visit.pk).exists()
    assert issue_join_signature(old_token) is None

    fresh = record_visit(canonical_invite_link(channel).token)
    assert fresh is not None
    assert validate_invite_visit(fresh[0].id) == fresh[0]

    link = canonical_invite_link(channel)
    link.is_active = False
    link.save(update_fields=["is_active", "updated_at"])
    assert validate_invite_visit(fresh[0].id) is None


@ENABLED
@pytest.mark.django_db
def test_link_classification_freezes_after_first_visit() -> None:
    channel, _actor = _channel()
    other, _other_actor = _channel()
    link = canonical_invite_link(channel)
    link.source = "tiktok"
    link.campaign = "october"
    link.content = "video"
    link.save(update_fields=["source", "campaign", "content", "updated_at"])
    record_visit(link.token)

    link.source = "instagram"
    with pytest.raises(AppendOnlyViolation):
        link.save(update_fields=["source", "updated_at"])
    with pytest.raises(AppendOnlyViolation):
        PartnerInviteLink.objects.filter(pk=link.pk).update(campaign="changed")
    link.partner_channel = other
    with pytest.raises(AppendOnlyViolation):
        link.save(update_fields=["partner_channel", "updated_at"])

    link.refresh_from_db()
    link.name = "October"
    link.bonus_amount = Decimal("10.000000")
    link.is_active = False
    link.save(update_fields=["name", "bonus_amount", "is_active", "updated_at"])
    link.refresh_from_db()
    assert link.source == "tiktok"
    assert link.campaign == "october"
    assert link.content == "video"
    assert link.name == "October"
    assert link.bonus_amount == Decimal("10.000000")
    assert link.is_active is False
    assert link.partner_channel_id == channel.pk


def test_invite_visit_protects_link_and_channel_already_protects() -> None:
    visit_field = InviteVisit._meta.get_field("invite_link")
    channel_field = PartnerInviteLink._meta.get_field("partner_channel")
    assert visit_field.remote_field.on_delete is models.PROTECT
    assert channel_field.remote_field.on_delete is models.PROTECT


@ENABLED
@pytest.mark.django_db(transaction=True)
def test_join_waits_while_the_link_row_is_locked() -> None:
    """PostgreSQL row lock: join's SELECT FOR UPDATE waits on the same link.

    The test database is PostgreSQL. A backend without row locks would not
    make ``issue_join_signature`` wait, and this assertion would fail.
    """
    channel, _actor = _channel()
    token = canonical_invite_link(channel).token
    locked = threading.Event()
    release = threading.Event()
    result: dict[str, object] = {}
    errors: list[BaseException] = []

    def hold() -> None:
        try:
            connection.close()
            with transaction.atomic():
                PartnerInviteLink.objects.select_for_update().get(token=token)
                locked.set()
                release.wait(timeout=5)
        except BaseException as exc:
            errors.append(exc)
            locked.set()
        finally:
            connection.close()

    def join() -> None:
        try:
            connection.close()
            locked.wait(timeout=5)
            started = time.monotonic()
            result["signed"] = issue_join_signature(token)
            result["elapsed"] = time.monotonic() - started
        except BaseException as exc:
            errors.append(exc)
        finally:
            connection.close()

    holder = threading.Thread(target=hold)
    holder.start()
    assert locked.wait(timeout=5)
    joiner = threading.Thread(target=join)
    joiner.start()
    time.sleep(0.4)
    release.set()
    holder.join(timeout=5)
    joiner.join(timeout=5)

    assert errors == []
    assert result["elapsed"] >= 0.3
    assert result["signed"]
    assert InviteVisit.objects.filter(invite_link__token=token).count() == 1
