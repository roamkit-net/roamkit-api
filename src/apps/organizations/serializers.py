"""Organization read serializers (ADR 020 / PR3)."""

from __future__ import annotations

from rest_framework import serializers

from apps.organizations.models import (
    InviteRole,
    Membership,
    Organization,
    OrganizationInvite,
)
from apps.organizations.permissions import permissions_for_role


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
