from datetime import timedelta

import pytest

from cs_agent.auth import otp, tokens
from cs_agent.db.models import VerificationCode
from cs_agent.services.common import RuleViolation


def test_send_and_verify(db, settings):
    sent = otp.send_code(db, settings, "(415) 555-0101")
    assert sent["sent_to"] == "***-***-0101"
    result = otp.verify_code(db, settings, "4155550101", "123456")
    assert result.ok and result.customer_id == 1
    # A code can only be used once.
    assert not otp.verify_code(db, settings, "4155550101", "123456").ok


def test_resend_is_rate_limited(db, settings):
    otp.send_code(db, settings, "4155550101")
    with pytest.raises(RuleViolation, match="wait"):
        otp.send_code(db, settings, "4155550101")


def test_wrong_code_attempts_are_limited(db, settings):
    otp.send_code(db, settings, "4155550101")
    for _ in range(settings.otp_max_attempts):
        assert not otp.verify_code(db, settings, "4155550101", "000000").ok
    # Even the right code is refused once attempts are exhausted.
    result = otp.verify_code(db, settings, "4155550101", "123456")
    assert not result.ok and "request a new code" in result.error


def test_expired_code(db, settings):
    otp.send_code(db, settings, "4155550101")
    record = db.query(VerificationCode).one()
    record.expires_at -= timedelta(seconds=settings.otp_ttl_seconds + 1)
    assert "expired" in otp.verify_code(db, settings, "4155550101", "123456").error


def test_unknown_phone_does_not_leak_on_send(db, settings):
    sent = otp.send_code(db, settings, "2125550199")
    assert sent["sent_to"] == "***-***-0199"
    result = otp.verify_code(db, settings, "2125550199", "123456")
    assert not result.ok and "couldn't find" in result.error


def test_invalid_phone(db, settings):
    with pytest.raises(RuleViolation):
        otp.send_code(db, settings, "12345")


def test_jwt_roundtrip(settings):
    token = tokens.issue_token(settings, 3)
    assert tokens.decode_token(settings, token) == 3
    with pytest.raises(tokens.InvalidToken):
        tokens.decode_token(settings, token + "x")
