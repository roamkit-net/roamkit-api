"""Organization read serializers (ADR 020 / PR3)."""

from __future__ import annotations

from rest_framework import serializers

from apps.organizations.models import (
    DeviceBinding,
    InviteRole,
    Membership,
    MembershipRole,
    Organization,
    OrganizationInvite,
)
from apps.organizations.permissions import permissions_for_role

# Roles assignable via PATCH members (owner only via transfer_ownership).
MANAGED_MEMBERSHIP_ROLES = (
    (MembershipRole.ADMIN, "Admin"),
    (MembershipRole.MEMBER, "Member"),
    (MembershipRole.VIEWER, "Viewer"),
)


class OrgPermissionsSerializer(serializers.Serializer):
    can_view = serializers.BooleanField()
    can_spend = serializers.BooleanField()
    can_manage_members = serializers.BooleanField()
    can_invite = serializers.BooleanField()
    can_transfer_ownership = serializers.BooleanField()
    can_archive_org = serializers.BooleanField()
    can_assign_esim = serializers.BooleanField()
    can_device_bind = serializers.BooleanField()


class OrganizationSerializer(serializers.ModelSerializer):
    """Organization plus the caller's role and permission flags."""

    my_role = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()
    # Informational only — never accept account_id as an authz input.
    account_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Organization
        fields = (
            "id",
            "name",
            "status",
            "account_id",
            "my_role",
            "permissions",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_my_role(self, obj: Organization) -> str:
        return self.context["my_role"]

    def get_permissions(self, obj: Organization) -> dict:
        perms = self.context.get("permissions")
        if perms is None:
            perms = permissions_for_role(self.context["my_role"])
        return OrgPermissionsSerializer(perms).data


class OrganizationCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=128, trim_whitespace=True)


class MembershipSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(read_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = Membership
        fields = (
            "id",
            "user_id",
            "user_email",
            "role",
            "status",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class MembershipRoleUpdateSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=MANAGED_MEMBERSHIP_ROLES)


class OrganizationTransferOwnershipSerializer(serializers.Serializer):
    new_owner_user_id = serializers.IntegerField(min_value=1)


class OrganizationTransferOwnershipResponseSerializer(serializers.Serializer):
    organization = OrganizationSerializer()
    new_owner_membership = MembershipSerializer()


class OrganizationInviteSerializer(serializers.ModelSerializer):
    invited_by_email = serializers.EmailField(
        source="invited_by.email",
        read_only=True,
    )

    class Meta:
        model = OrganizationInvite
        fields = (
            "id",
            "organization_id",
            "email",
            "email_normalized",
            "role",
            "status",
            "expires_at",
            "invited_by_email",
            "accepted_at",
            "revoked_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class OrganizationInviteCreateSerializer(serializers.Serializer):
    email = serializers.EmailField()
    role = serializers.ChoiceField(
        choices=InviteRole.choices,
        default=InviteRole.MEMBER,
    )


class OrganizationInviteCreateResponseSerializer(serializers.Serializer):
    invite = OrganizationInviteSerializer()
    token = serializers.CharField(
        help_text="Single-use invite token; shown only at create/refresh time."
    )
    created = serializers.BooleanField()


class OrganizationInviteAcceptSerializer(serializers.Serializer):
    token = serializers.CharField()


class OrganizationInviteAcceptResponseSerializer(serializers.Serializer):
    membership = MembershipSerializer()
    organization_id = serializers.UUIDField()
    already_accepted = serializers.BooleanField()


class DeviceBindingSerializer(serializers.ModelSerializer):
    organization_id = serializers.UUIDField(read_only=True)
    esim_id = serializers.IntegerField(read_only=True)
    iccid = serializers.CharField(source="esim.iccid", read_only=True)

    class Meta:
        model = DeviceBinding
        fields = (
            "id",
            "organization_id",
            "esim_id",
            "iccid",
            "device_external_id",
            "uem_device_guid",
            "status",
            "bound_by_id",
            "unbound_by_id",
            "unbound_at",
            "replaced_by_id",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class DeviceBindingCreateSerializer(serializers.Serializer):
    esim_id = serializers.IntegerField(min_value=1)
    replace = serializers.BooleanField(
        required=False,
        default=False,
        help_text=(
            "When true, replace an existing active binding on this eSIM "
            "(old → replaced, new → active)."
        ),
    )

    def validate(self, attrs: dict) -> dict:
        if "account_id" in self.initial_data:
            raise serializers.ValidationError(
                {
                    "account_id": (
                        "Client-supplied account_id is not accepted; "
                        "authorization uses organization_id path only."
                    )
                }
            )
        if "device_external_id" in self.initial_data:
            raise serializers.ValidationError(
                {
                    "device_external_id": (
                        "Client-supplied device_external_id is not accepted; "
                        "RoamKit issues this id on create."
                    )
                }
            )
        return attrs


class DeviceStatusEsimSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    iccid = serializers.CharField()
    status = serializers.CharField()


class DeviceStatusUsageSerializer(serializers.Serializer):
    data_remaining = serializers.CharField(allow_null=True)
    data_used = serializers.CharField(allow_null=True)
    expires_at = serializers.DateTimeField(allow_null=True)


class DeviceStatusAutoTopupSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()


class DeviceStatusCoverageSummarySerializer(serializers.Serializer):
    """Light coverage affordance for status (no country list)."""

    available = serializers.BooleanField()
    country_count = serializers.IntegerField()


class DeviceStatusPlanSerializer(serializers.Serializer):
    """Purchase-time plan metadata from Order snapshot (nullable parent field)."""

    title = serializers.CharField(allow_null=True)
    data_allowance = serializers.CharField(allow_null=True)
    validity_days = serializers.IntegerField(allow_null=True)
    country_code = serializers.CharField(allow_null=True)
    coverage_type = serializers.CharField(allow_null=True)
    location_title = serializers.CharField(allow_null=True)
    coverage_summary = DeviceStatusCoverageSummarySerializer(allow_null=True)


class DeviceStatusSerializer(serializers.Serializer):
    """Read-only UEM status contract (cached inventory/usage; no provider call)."""

    device_external_id = serializers.CharField()
    binding_status = serializers.CharField()
    esim = DeviceStatusEsimSerializer()
    usage = DeviceStatusUsageSerializer()
    auto_topup = DeviceStatusAutoTopupSerializer()
    plan = DeviceStatusPlanSerializer(allow_null=True)
    checked_at = serializers.DateTimeField()


class DeviceCoverageCountrySerializer(serializers.Serializer):
    """Stable purchase-time coverage row (no provider raw fields)."""

    country_code = serializers.CharField()
    country_name = serializers.CharField(allow_null=True)
    operators = serializers.ListField(
        child=serializers.CharField(),
        allow_empty=True,
    )


class DeviceCoverageSerializer(serializers.Serializer):
    """Device-facing coverage list from Order.coverage_snapshot only."""

    device_external_id = serializers.CharField()
    coverage_type = serializers.CharField(allow_null=True)
    coverage = DeviceCoverageCountrySerializer(many=True, allow_null=True)
    checked_at = serializers.DateTimeField()


class DeviceBindingCredentialResponseSerializer(serializers.Serializer):
    """Binding plus one-time plaintext credential (create / rotate only)."""

    binding = DeviceBindingSerializer()
    credential = serializers.CharField(
        help_text="Opaque device secret; shown only at issue/rotate time."
    )


def _reject_client_scope_fields(initial_data) -> None:
    if "organization_id" in initial_data:
        raise serializers.ValidationError(
            {
                "organization_id": (
                    "organization_id is not accepted on device credential "
                    "endpoints; credential scopes the binding."
                )
            }
        )
    if "account_id" in initial_data:
        raise serializers.ValidationError(
            {"account_id": "Client-supplied account_id is not accepted."}
        )
    if "esim_id" in initial_data:
        raise serializers.ValidationError(
            {
                "esim_id": (
                    "esim_id is not accepted; coverage/status resolve the "
                    "eSIM via the authenticated device binding only."
                )
            }
        )


class DeviceCredentialRequestSerializer(serializers.Serializer):
    """PR18 device credential body (coverage; never in URL)."""

    device_external_id = serializers.CharField(max_length=64)
    credential = serializers.CharField(max_length=256)

    def validate(self, attrs: dict) -> dict:
        _reject_client_scope_fields(self.initial_data)
        return attrs


class DeviceStatusRequestSerializer(serializers.Serializer):
    """Device status body — exactly one of PR18 or serial shape (ADR 021 C″).

    PR18: ``device_external_id`` + ``credential``
    Serial: ``device_serial`` only

    Mixed / incomplete / legacy ``fleet_*`` fields → 400 (no guessing).
    """

    device_external_id = serializers.CharField(
        max_length=64, required=False, allow_blank=False
    )
    credential = serializers.CharField(
        max_length=256, required=False, allow_blank=False
    )
    device_serial = serializers.CharField(
        max_length=128, required=False, allow_blank=False
    )

    _PR18_KEYS = ("device_external_id", "credential")
    _SERIAL_KEYS = ("device_serial",)
    _REMOVED_FLEET_KEYS = ("fleet_external_id", "fleet_credential")

    def validate(self, attrs: dict) -> dict:
        _reject_client_scope_fields(self.initial_data)
        data = self.initial_data or {}

        if any(key in data for key in self._REMOVED_FLEET_KEYS):
            raise serializers.ValidationError(
                "fleet_external_id / fleet_credential are not accepted on v1 "
                "device status; use device_serial or PR18 credentials."
            )

        pr18_present = any(key in data for key in self._PR18_KEYS)
        serial_present = any(key in data for key in self._SERIAL_KEYS)

        if pr18_present and serial_present:
            raise serializers.ValidationError(
                "Cannot mix PR18 fields (device_external_id, credential) with "
                "serial field (device_serial)."
            )

        if pr18_present:
            missing = [
                key for key in self._PR18_KEYS if not str(data.get(key) or "").strip()
            ]
            if missing:
                raise serializers.ValidationError(
                    {
                        key: "This field is required for PR18 device auth."
                        for key in missing
                    }
                )
            return {
                "auth_shape": "pr18",
                "device_external_id": str(data["device_external_id"]).strip(),
                "credential": str(data["credential"]),
            }

        if serial_present:
            serial = str(data.get("device_serial") or "").strip()
            if not serial:
                raise serializers.ValidationError(
                    {"device_serial": "This field is required for serial status."}
                )
            return {
                "auth_shape": "serial",
                "device_serial": serial,
            }

        raise serializers.ValidationError(
            "Provide either device_external_id+credential (PR18) or "
            "device_serial (ADR 021 Option C″)."
        )
