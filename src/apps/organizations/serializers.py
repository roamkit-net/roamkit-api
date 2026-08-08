"""Organization read serializers (ADR 020 / PR3)."""

from __future__ import annotations

from rest_framework import serializers

from apps.organizations.models import Membership, Organization
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
