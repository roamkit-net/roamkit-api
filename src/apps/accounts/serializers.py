"""Auth serializers."""

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from apps.accounts.services.registration import RegistrationError, register_user

User = get_user_model()


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8, max_length=128)

    def validate_password(self, value: str) -> str:
        validate_password(value)
        return value

    def create(self, validated_data: dict) -> User:
        try:
            return register_user(
                email=validated_data["email"],
                password=validated_data["password"],
            )
        except RegistrationError as exc:
            raise serializers.ValidationError({"email": exc.message}) from exc


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "email", "created_at", "updated_at")
        read_only_fields = fields
