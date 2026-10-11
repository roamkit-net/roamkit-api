"""Transactional auth email helpers."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.tokens import account_activation_token, password_reset_token

User = get_user_model()


def build_frontend_link(path: str, *, uid: str, token: str) -> str:
    base = settings.FRONTEND_BASE_URL.rstrip("/")
    return f"{base}{path}?uid={uid}&token={token}"


def uid_for_user(user: User) -> str:
    return urlsafe_base64_encode(force_bytes(user.pk))


def send_activation_email(user: User, link: str | None = None) -> None:
    """Send the account confirmation / set-password email."""
    uid = uid_for_user(user)
    token = account_activation_token.make_token(user)
    activation_link = link or build_frontend_link(
        "/set-password",
        uid=uid,
        token=token,
    )
    context = {
        "user": user,
        "link": activation_link,
        "frontend_base_url": settings.FRONTEND_BASE_URL,
    }
    subject = render_to_string(
        "accounts/email/activation_subject.txt",
        context,
    ).strip()
    body = render_to_string("accounts/email/activation_body.txt", context)
    send_mail(
        subject,
        body,
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
        fail_silently=False,
    )


def send_signup_account_exists_email(user: User) -> None:
    """Tell the mailbox owner the address is already registered.

    No reset token. Login and forgot-password stay on the web.
    """
    base = settings.FRONTEND_BASE_URL.rstrip("/")
    context = {
        "user": user,
        "login_link": f"{base}/login",
        "reset_link": f"{base}/forgot-password",
        "frontend_base_url": settings.FRONTEND_BASE_URL,
    }
    subject = render_to_string(
        "accounts/email/signup_account_exists_subject.txt",
        context,
    ).strip()
    body = render_to_string(
        "accounts/email/signup_account_exists_body.txt",
        context,
    )
    send_mail(
        subject,
        body,
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
        fail_silently=False,
    )


def send_signup_code_email(user: User, code: str) -> None:
    """Send the 4-digit mobile signup code. The database stores only a hash."""
    context = {
        "user": user,
        "code": code,
        "frontend_base_url": settings.FRONTEND_BASE_URL,
    }
    subject = render_to_string(
        "accounts/email/signup_code_subject.txt",
        context,
    ).strip()
    body = render_to_string("accounts/email/signup_code_body.txt", context)
    send_mail(
        subject,
        body,
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
        fail_silently=False,
    )


def send_password_reset_email(user: User, link: str | None = None) -> None:
    """Send the forgot-password reset email."""
    uid = uid_for_user(user)
    token = password_reset_token.make_token(user)
    reset_link = link or build_frontend_link(
        "/reset-password",
        uid=uid,
        token=token,
    )
    context = {
        "user": user,
        "link": reset_link,
        "frontend_base_url": settings.FRONTEND_BASE_URL,
    }
    subject = render_to_string(
        "accounts/email/password_reset_subject.txt",
        context,
    ).strip()
    body = render_to_string("accounts/email/password_reset_body.txt", context)
    send_mail(
        subject,
        body,
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
        fail_silently=False,
    )
