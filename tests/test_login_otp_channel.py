from unittest.mock import Mock

from app.models import models, schemas
from app.services.auth_service import AuthService


def test_phone_login_sends_otp_to_supplied_phone(db_session):
    user = models.User(phone="+2348012345678", email="owner@example.com", name="Owner")
    db_session.add(user)
    db_session.commit()
    otp_service = Mock()
    otp_service.request_login.return_value = "whatsapp"

    channel = AuthService(db_session, otp_service).request_login(
        schemas.OTPPhoneRequest(phone="08012345678"),
    )

    assert channel == "whatsapp"
    otp_service.request_login.assert_called_once_with("+2348012345678")


def test_email_login_matches_legacy_mixed_case_address(db_session):
    user = models.User(phone="+2348098765432", email="Owner@Example.com", name="Owner")
    db_session.add(user)
    db_session.commit()
    otp_service = Mock()
    otp_service.request_login.return_value = "email"

    channel = AuthService(db_session, otp_service).request_login(
        schemas.OTPEmailRequest(email="owner@example.com"),
    )

    assert channel == "email"
    otp_service.request_login.assert_called_once_with("owner@example.com")


def test_email_login_verification_matches_legacy_mixed_case_address(db_session):
    user = models.User(phone="+2348098765432", email="Owner@Example.com", name="Owner")
    db_session.add(user)
    db_session.commit()
    otp_service = Mock()
    otp_service.verify_otp.return_value = True

    tokens = AuthService(db_session, otp_service).verify_login(
        schemas.LoginVerify(email="owner@example.com", otp="123456"),
    )

    assert tokens.user_id == user.id
    otp_service.verify_otp.assert_called_once_with("owner@example.com", "123456", "login")


def test_phone_login_resend_stays_on_whatsapp(db_session):
    otp_service = Mock()
    otp_service.resend_otp.return_value = "whatsapp"

    channel = AuthService(db_session, otp_service).resend_otp(
        schemas.OTPResend(phone="08012345678", purpose="login"),
    )

    assert channel == "whatsapp"
    otp_service.resend_otp.assert_called_once_with("+2348012345678", "login")
