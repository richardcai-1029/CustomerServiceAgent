"""One-time SMS verification codes (SMS delivery is simulated via logging)."""

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from cs_agent.config import Settings
from cs_agent.db.models import VerificationCode
from cs_agent.services.accounts import find_by_phone
from cs_agent.services.common import RuleViolation, mask_phone, normalize_phone, now

log = logging.getLogger(__name__)


class ResendTooSoon(RuleViolation):
    """A code was sent moments ago; it is still valid, so the caller can wait for it."""

    def __init__(self, phone: str, wait: int):
        super().__init__(f"A code was sent recently. Please wait {wait} seconds before requesting another.")
        self.phone = phone


@dataclass
class VerifyResult:
    ok: bool
    customer_id: int | None = None
    customer_name: str | None = None
    error: str | None = None


def _hash(phone: str, code: str) -> str:
    return hashlib.sha256(f"{phone}:{code}".encode()).hexdigest()


def _latest(session: Session, phone: str) -> VerificationCode | None:
    stmt = (
        select(VerificationCode)
        .where(VerificationCode.phone == phone)
        .order_by(VerificationCode.created_at.desc(), VerificationCode.id.desc())
        .limit(1)
    )
    return session.scalar(stmt)


def send_code(session: Session, settings: Settings, phone: str) -> dict:
    """Issue a code for `phone`.

    The response is identical whether or not the phone is registered, so this
    endpoint cannot be used to probe which numbers have accounts.
    """
    phone = normalize_phone(phone)
    if len(phone) != 10:
        raise RuleViolation("Please provide a valid 10-digit phone number.")

    latest = _latest(session, phone)
    t = now()
    # Throttle only while the previous code is still usable; after it was used or has
    # expired, a fresh code may be sent right away.
    if latest is not None and not latest.consumed and t < latest.expires_at:
        wait = settings.otp_resend_seconds - int((t - latest.created_at).total_seconds())
        if wait > 0:
            raise ResendTooSoon(phone, wait)

    code = settings.dev_otp_code if settings.dev_mode else f"{secrets.randbelow(10**6):06d}"
    session.add(VerificationCode(phone=phone, code_hash=_hash(phone, code), created_at=t,
                                 expires_at=t + timedelta(seconds=settings.otp_ttl_seconds)))
    log.info("[simulated SMS to %s] Your NovaTech verification code is %s", mask_phone(phone), code)
    result = {
        "phone": phone,
        "sent_to": mask_phone(phone),
        "expires_in_seconds": settings.otp_ttl_seconds,
    }
    if settings.dev_mode:
        result["dev_hint"] = f"Dev mode: the code is {code}."
    return result


def verify_code(session: Session, settings: Settings, phone: str, code: str) -> VerifyResult:
    phone = normalize_phone(phone)
    record = _latest(session, phone)
    if record is None or record.consumed:
        return VerifyResult(False, error="There is no active code for this number. Please request a new one.")
    if now() > record.expires_at:
        return VerifyResult(False, error="That code has expired. Please request a new one.")
    if record.attempts >= settings.otp_max_attempts:
        return VerifyResult(False, error="Too many incorrect attempts. Please request a new code.")

    if not secrets.compare_digest(record.code_hash, _hash(phone, code.strip())):
        record.attempts += 1
        left = settings.otp_max_attempts - record.attempts
        if left <= 0:
            return VerifyResult(False, error="Incorrect code. No attempts left - please request a new code.")
        return VerifyResult(False, error=f"Incorrect code. {left} attempt(s) left.")

    record.consumed = True
    customer = find_by_phone(session, phone)
    if customer is None:
        # The caller proved they own the phone, so revealing this is safe.
        return VerifyResult(False, error="We couldn't find a NovaTech account for that phone number.")
    return VerifyResult(True, customer_id=customer.id, customer_name=customer.name)
