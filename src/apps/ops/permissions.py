"""Staff-only permission for Operations Dashboard endpoints."""

from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView


class IsStaff(BasePermission):
    """Require authenticated user with ``is_staff=True``."""

    message = "Staff access required."

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = request.user
        return bool(user and user.is_authenticated and getattr(user, "is_staff", False))
