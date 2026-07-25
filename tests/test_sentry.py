from config.sentry import init_sentry


def test_init_sentry_noop_without_dsn(monkeypatch) -> None:
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert init_sentry(environment="test") is False


def test_init_sentry_enabled_with_dsn(monkeypatch) -> None:
    monkeypatch.setenv("SENTRY_DSN", "https://public@example.com/1")
    monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "0")

    # Avoid network: stub sentry_sdk.init
    import config.sentry as sentry_mod

    calls: list[dict] = []

    class _FakeSdk:
        @staticmethod
        def init(**kwargs):
            calls.append(kwargs)

    class _FakeDjango:
        pass

    class _FakeCelery:
        pass

    monkeypatch.setattr(
        sentry_mod,
        "init_sentry",
        sentry_mod.init_sentry,
    )

    import sys
    import types

    fake_sdk = types.ModuleType("sentry_sdk")
    fake_sdk.init = _FakeSdk.init  # type: ignore[attr-defined]
    fake_django = types.ModuleType("sentry_sdk.integrations.django")
    fake_django.DjangoIntegration = _FakeDjango  # type: ignore[attr-defined]
    fake_celery = types.ModuleType("sentry_sdk.integrations.celery")
    fake_celery.CeleryIntegration = _FakeCelery  # type: ignore[attr-defined]
    fake_integrations = types.ModuleType("sentry_sdk.integrations")

    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations", fake_integrations)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.django", fake_django)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.celery", fake_celery)

    assert init_sentry(environment="production") is True
    assert calls and calls[0]["environment"] == "production"
    assert calls[0]["dsn"] == "https://public@example.com/1"
