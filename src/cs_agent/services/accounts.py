"""Account-level queries: profile, balance, refunds, password reset."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from cs_agent.db.models import Customer, PasswordResetRequest, Refund
from cs_agent.services.common import (
    NotFoundError,
    mask_phone,
    money,
    normalize_order_id,
    normalize_phone,
    now,
)
from cs_agent.services.orders import refund_view


def get_customer(session: Session, customer_id: int) -> Customer:
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise NotFoundError("Customer account not found.")
    return customer


def find_by_phone(session: Session, phone: str) -> Customer | None:
    return session.scalar(select(Customer).where(Customer.phone == normalize_phone(phone)))


def balance(session: Session, customer_id: int) -> dict:
    customer = get_customer(session, customer_id)
    return {"balance": money(customer.balance), "currency": "USD"}


def refunds(session: Session, customer_id: int, order_id: str | None = None) -> list[dict]:
    stmt = select(Refund).where(Refund.customer_id == customer_id)
    if order_id:
        stmt = stmt.where(Refund.order_id == normalize_order_id(order_id))
    rows = session.scalars(stmt.order_by(Refund.created_at.desc())).all()
    return [refund_view(r) for r in rows]


def request_password_reset(session: Session, customer_id: int) -> dict:
    """Send (simulated) a password-reset link to the phone on file."""
    customer = get_customer(session, customer_id)
    session.add(PasswordResetRequest(customer_id=customer.id, sent_to=customer.phone,
                                     created_at=now()))
    return {
        "sent_to": mask_phone(customer.phone),
        "message": "A password reset link was sent by SMS. It expires in 30 minutes.",
    }
