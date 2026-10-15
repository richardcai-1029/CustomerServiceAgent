"""Return & refund requests."""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cs_agent.db.models import Order, OrderStatus, ReturnItem, ReturnRequest
from cs_agent.services.common import RuleViolation, fmt_time, money, new_id, now
from cs_agent.services.orders import STATUS_LABELS, _refund_payment, get_order

RETURN_REASONS = {
    "defective": "Item is defective or doesn't work",
    "damaged_in_transit": "Item arrived damaged",
    "wrong_item": "Received the wrong item",
    "not_as_described": "Item is not as described",
    "changed_mind": "Changed my mind / no longer needed",
    "other": "Other reason",
}

REFUND_METHODS = {
    "original": "Original payment method",
    "balance": "Store account balance (instant once approved)",
}


def check_return_eligibility(order: Order, window_days: int) -> None:
    if order.status != OrderStatus.DELIVERED:
        raise RuleViolation(
            f"Order {order.id} is '{STATUS_LABELS[order.status]}'. Only delivered orders "
            "can be returned."
        )
    deadline = order.delivered_at + timedelta(days=window_days)
    if now() > deadline:
        raise RuleViolation(
            f"Order {order.id} was delivered on {fmt_time(order.delivered_at)}; the "
            f"{window_days}-day return window closed on {fmt_time(deadline)}. "
            "Faulty items may still be covered by the warranty."
        )


def eligible_orders(session: Session, customer_id: int, window_days: int) -> list[Order]:
    cutoff = now() - timedelta(days=window_days)
    stmt = (
        select(Order)
        .where(
            Order.customer_id == customer_id,
            Order.status == OrderStatus.DELIVERED,
            Order.delivered_at >= cutoff,
        )
        .order_by(Order.delivered_at.desc())
    )
    return list(session.scalars(stmt).all())


def load_returnable_order(session: Session, customer_id: int, order_id: str,
                          window_days: int) -> Order:
    order = get_order(session, customer_id, order_id)
    check_return_eligibility(order, window_days)
    return order


def create_return(session: Session, customer_id: int, order_id: str, item_ids: list[int],
                  reason: str, comment: str | None, refund_method: str,
                  window_days: int) -> dict:
    order = load_returnable_order(session, customer_id, order_id, window_days)
    if reason not in RETURN_REASONS:
        raise RuleViolation(f"Unknown return reason '{reason}'.")
    if refund_method not in REFUND_METHODS:
        raise RuleViolation(f"Unknown refund method '{refund_method}'.")
    items = [i for i in order.items if i.id in set(item_ids)]
    if not items or len(items) != len(set(item_ids)):
        raise RuleViolation("Please choose items that belong to this order.")

    amount = sum((Decimal(i.unit_price) * i.quantity for i in items), Decimal("0"))
    rma = ReturnRequest(id=new_id("RMA"), order_id=order.id, customer_id=customer_id,
                        reason=reason, comment=comment, refund_method=refund_method,
                        refund_amount=amount, status="awaiting_item", created_at=now())
    session.add(rma)
    session.flush()
    for item in items:
        session.add(ReturnItem(return_id=rma.id, order_item_id=item.id, quantity=item.quantity))
    order.status = OrderStatus.RETURN_REQUESTED
    method = "balance" if refund_method == "balance" else order.payment_method
    refund = _refund_payment(session, order, amount, method=method, source="return",
                             return_id=rma.id, status="awaiting_return")
    return {
        "return_id": rma.id,
        "order_id": order.id,
        "items": [f"{i.quantity} x {i.product_name}" for i in items],
        "refund_amount": money(amount),
        "refund_method": REFUND_METHODS[refund_method],
        "refund_id": refund["refund_id"],
        "next_steps": (
            "A prepaid return label has been emailed to you. Please ship the item(s) within "
            "7 days; the refund is issued once we receive and inspect them."
        ),
    }
