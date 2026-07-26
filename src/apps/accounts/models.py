"""Custom user model — email as identity."""

from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models


class UserManager(BaseUserManager):
    """Manager for email-based users."""

    def create_user(
        self, email: str, password: str | None = None, **extra_fields
    ) -> "User":
        if not email:
            raise ValueError("Users must have an email address")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(
        self, email: str, password: str | None = None, **extra_fields
    ) -> "User":
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """RoamKit user identified by email."""

    class LastLoginProvider(models.TextChoices):
        PASSWORD = "password", "password"
        GOOGLE = "google", "google"

    email = models.EmailField(unique=True, db_index=True)
    wallet_address = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
    )
    google_sub = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        unique=True,
        db_index=True,
    )
    google_name = models.CharField(
        max_length=255, blank=True, default="", db_default=""
    )
    google_picture = models.URLField(
        max_length=2048, blank=True, default="", db_default=""
    )
    last_login_provider = models.CharField(
        max_length=32,
        choices=LastLoginProvider.choices,
        null=True,
        blank=True,
    )
    last_google_login_at = models.DateTimeField(null=True, blank=True)
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        ordering = ["email"]

    def __str__(self) -> str:
        return self.email
