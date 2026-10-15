"""Shared helpers for the business service layer."""

import re
import secrets
from datetime import datetime
from decimal import Decimal


class BusinessError(Exception):
    """A business-rule failure whose message is safe to show to the customer."""


class NotFoundError(BusinessError):
    pass


class RuleViolation(BusinessError):
    pass


def now() -> datetime:
    # Naive local time keeps MySQL DATETIME columns and demo output simple.
    return datetime.now().replace(microsecond=0)


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(3).upper()}"


def money(value: Decimal) -> str:
    return f"${value:,.2f}"


def fmt_time(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%d %H:%M") if value else None


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    # Accept a leading US country code.
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def mask_phone(phone: str) -> str:
    return f"***-***-{phone[-4:]}"


ORDER_ID_RE = re.compile(r"\b(?:ORD[-\s]?)?(\d{4})\b", re.IGNORECASE)


def normalize_order_id(order_id: str) -> str:
    """Accept 'ORD-1001', 'ord1001' or '1001' and return 'ORD-1001'."""
    match = ORDER_ID_RE.search(order_id.strip())
    if not match:
        raise NotFoundError(f"'{order_id}' does not look like an order number (e.g. ORD-1001).")
    return f"ORD-{match.group(1)}"
