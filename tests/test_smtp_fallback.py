"""SMTP provider fallback: a failed primary (ZeptoMail 535) must fall through to
the next configured provider (Brevo) so transactional mail still sends.
"""
import smtplib

import pytest

from app.core.config import settings
from app.utils import smtp as smtp_mod


@pytest.fixture
def two_providers(monkeypatch):
    """Configure ZeptoMail (primary) + Brevo (fallback), nothing else."""
    monkeypatch.setattr(settings, "SMTP_HOST_ZEP", "smtp.zeptomail.com", raising=False)
    monkeypatch.setattr(settings, "SMTP_USER_ZEP", "emailapikey", raising=False)
    monkeypatch.setattr(settings, "SMTP_PASSWORD_ZEP", "zepto-token", raising=False)
    monkeypatch.setattr(settings, "SMTP_USER", None, raising=False)
    monkeypatch.setattr(settings, "SMTP_PASSWORD", None, raising=False)
    monkeypatch.setattr(settings, "BREVO_SMTP_LOGIN", "brevo-login", raising=False)
    monkeypatch.setattr(settings, "BREVO_SMTP_KEY", "xsmtpsib-brevo-key", raising=False)
    monkeypatch.setattr(settings, "BREVO_API_KEY", "xkeysib-api-key", raising=False)
    monkeypatch.setattr(settings, "FROM_EMAIL", "noreply@suoops.com", raising=False)


def _install_fake_smtp(monkeypatch, sent_hosts, fail_hosts):
    class _FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, user, password):
            if self.host in fail_hosts:
                raise smtplib.SMTPAuthenticationError(535, b"Authentication Failed")

        def send_message(self, msg):
            sent_hosts.append(self.host)

    monkeypatch.setattr(smtp_mod.smtplib, "SMTP", _FakeSMTP)


def test_configs_are_ordered_zepto_then_brevo(two_providers):
    hosts = [c[0] for c in smtp_mod.get_smtp_configs()]
    assert hosts == ["smtp.zeptomail.com", "smtp-relay.brevo.com"]


def test_rest_api_key_is_not_used_as_smtp_password(two_providers, monkeypatch):
    monkeypatch.setattr(settings, "BREVO_SMTP_KEY", None, raising=False)

    configs = smtp_mod.get_smtp_configs()

    assert [config[0] for config in configs] == ["smtp.zeptomail.com"]


def test_legacy_smtp_key_in_api_key_setting_is_supported(two_providers, monkeypatch):
    monkeypatch.setattr(settings, "BREVO_SMTP_KEY", None, raising=False)
    monkeypatch.setattr(settings, "BREVO_API_KEY", "xsmtpsib-legacy-key", raising=False)

    configs = smtp_mod.get_smtp_configs()

    assert configs[-1][0] == "smtp-relay.brevo.com"
    assert configs[-1][3] == "xsmtpsib-legacy-key"


def test_falls_back_to_brevo_when_zepto_auth_fails(two_providers, monkeypatch):
    sent: list[str] = []
    _install_fake_smtp(monkeypatch, sent, fail_hosts={"smtp.zeptomail.com"})

    ok = smtp_mod.send_email_with_fallback(
        "user@example.com", "Subject", "<b>hi</b>", "hi", check_suppression=False
    )
    assert ok is True
    # ZeptoMail failed login (no send), Brevo delivered.
    assert sent == ["smtp-relay.brevo.com"]


def test_uses_primary_when_it_works(two_providers, monkeypatch):
    sent: list[str] = []
    _install_fake_smtp(monkeypatch, sent, fail_hosts=set())

    ok = smtp_mod.send_email_with_fallback(
        "user@example.com", "Subject", None, "hi", check_suppression=False
    )
    assert ok is True
    assert sent == ["smtp.zeptomail.com"]  # no fallback needed


def test_returns_false_when_all_providers_fail(two_providers, monkeypatch):
    sent: list[str] = []
    _install_fake_smtp(
        monkeypatch, sent, fail_hosts={"smtp.zeptomail.com", "smtp-relay.brevo.com"}
    )

    ok = smtp_mod.send_email_with_fallback(
        "user@example.com", "Subject", None, "hi", check_suppression=False
    )
    assert ok is False
    assert sent == []
