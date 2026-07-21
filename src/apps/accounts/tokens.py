"""Token generators for account activation and password reset."""

from datetime import datetime

from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.crypto import constant_time_compare
from django.utils.http import base36_to_int


class TimedTokenGenerator(PasswordResetTokenGenerator):
    """PasswordResetTokenGenerator with an explicit timeout and purpose salt."""

    key_salt = "apps.accounts.tokens.TimedTokenGenerator"
    timeout_seconds = 60 * 60

    def check_token(self, user, token: str) -> bool:
        if not (user and token):
            return False
        try:
            ts_b36, _ = token.split("-")
        except ValueError:
            return False
        try:
            ts = base36_to_int(ts_b36)
        except ValueError:
            return False

        for secret in [self.secret, *self.secret_fallbacks]:
            if constant_time_compare(
                self._make_token_with_timestamp(user, ts, secret),
                token,
            ):
                break
        else:
            return False

        if (self._num_seconds(self._now()) - ts) > self.timeout_seconds:
            return False
        return True

    def _now(self) -> datetime:
        return datetime.now()


class AccountActivationTokenGenerator(TimedTokenGenerator):
    key_salt = "apps.accounts.tokens.AccountActivationTokenGenerator"
    timeout_seconds = 60 * 60 * 24  # 24 hours


class PasswordResetTokenGeneratorForEmail(TimedTokenGenerator):
    key_salt = "apps.accounts.tokens.PasswordResetTokenGeneratorForEmail"
    timeout_seconds = 60 * 60  # 1 hour


account_activation_token = AccountActivationTokenGenerator()
password_reset_token = PasswordResetTokenGeneratorForEmail()
