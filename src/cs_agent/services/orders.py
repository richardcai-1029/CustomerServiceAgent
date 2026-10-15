"""Order queries and pre-shipment changes (cancel, address change)."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cs_agent.db.models import Customer, Order, OrderStatus, Refund
from cs_agent.services.common import (
    NotFoundError,
    RuleViolation,
    fmt_time,
    money,
    new_id,
    normalize_order_id,
    normalize_phone,
    now,
)

STATUS_LABELS = {
    OrderStatus.PENDING_PAYMENT: "Awaiting payment",
    OrderStatus.PAID: "Paid - preparing for shipment",
    OrderStatus.SHIPPED: "Shipped - in transit",
    OrderStatus.DELIVERED: "Delivered",
    OrderStatus.CANCELLED: "Cancelled",
    OrderStatus.RETURN_REQUESTED: "Return requested",
    OrderStatus.REFUNDED: "Refunded",
}


def get_order(session: Session, customer_id: int, order_id: str) -> Order:
    """Load an order owned by the customer; other customers' orders look 'not found'."""
    oid = normalize_order_id(order_id)
    order = session.get(Order, oid)
    if order is None or order.customer_id != customer_id:
        raise NotFoundError(f"No order {oid} was found on this account.")
    return order


def order_summary(order: Order) -> dict:
    return {
        "order_id": order.id,
        "status": order.status,
        "status_label": STATUS_LABELS[order.status],
        "placed_at": fmt_time(order.created_at),
        "total": money(order.total_amount),
        "items": [f"{i.quantity} x {i.product_name}" for i in order.items],
    }


def order_details(order: Order) -> dict:
    details = order_summary(order)
    details.update(
        payment_method=order.payment_method,
        recipient=f"{order.recipient_name} ({order.recipient_phone})",
        shipping_address=order.shipping_address,
        shipped_at=fmt_time(order.shipped_at),
        delivered_at=fmt_time(order.delivered_at),
        items=[
            {
                "item_id": i.id,
                "product": i.product_name,
                "sku": i.sku,
                "quantity": i.quantity,
                "unit_price": money(i.unit_price),
            }
            for i in order.items
        ],
    )
    return details


def list_orders(session: Session, customer_id: int, status: str | None = None) -> list[dict]:
    stmt = select(Order).where(Order.customer_id == customer_id)
    if status:
        stmt = stmt.where(Order.status == status)
    orders = session.scalars(stmt.order_by(Order.created_at.desc())).all()
    return [order_summary(o) for o in orders]


def tracking(session: Session, customer_id: int, order_id: str) -> dict:
    order = get_order(session, customer_id, order_id)
    if order.status in OrderStatus.PRE_SHIPMENT:
        return {
            "order_id": order.id,
            "status_label": STATUS_LABELS[order.status],
            "message": "This order has not shipped yet, so there is no tracking information.",
        }
    if order.carrier is None:
        return {"order_id": order.id, "status_label": STATUS_LABELS[order.status],
                "message": "This order never shipped."}
    return {
        "order_id": order.id,
        "status_label": STATUS_LABELS[order.status],
        "carrier": order.carrier,
        "tracking_number": order.tracking_number,
        "events": [
            {"time": fmt_time(e.occurred_at), "location": e.location, "event": e.description}
            for e in order.tracking_events
        ],
    }


# --- Cancellation ---------------------------------------------------------


def check_cancellable(session: Session, customer_id: int, order_id: str) -> Order:
    order = get_order(session, customer_id, order_id)
    if order.status not in OrderStatus.PRE_SHIPMENT:
        raise RuleViolation(
            f"Order {order.id} is '{STATUS_LABELS[order.status]}' and can no longer be "
            "cancelled. Only orders that have not shipped can be cancelled."
        )
    return order


def cancel_order(session: Session, customer_id: int, order_id: str) -> dict:
    order = check_cancellable(session, customer_id, order_id)
    was_paid = order.status == OrderStatus.PAID
    order.status = OrderStatus.CANCELLED
    order.cancelled_at = now()
    result: dict = {"order_id": order.id, "status": order.status}
    if was_paid:
        result["refund"] = _refund_payment(session, order, Decimal(order.total_amount),
                                           method=order.payment_method, source="cancellation")
    return result


def _refund_payment(session: Session, order: Order, amount: Decimal, *, method: str,
                    source: str, return_id: str | None = None,
                    status: str | None = None) -> dict:
    """Record a refund. Balance refunds settle instantly; card refunds take days."""
    t = now()
    if status is None:
        status = "completed" if method == "balance" else "processing"
    refund = Refund(id=new_id("RF"), order_id=order.id, customer_id=order.customer_id,
                    return_id=return_id, amount=amount, method=method, source=source,
                    status=status, created_at=t,
                    completed_at=t if status == "completed" else None)
    if status == "completed" and method == "balance":
        customer = session.get(Customer, order.customer_id)
        customer.balance = Decimal(customer.balance) + amount
    session.add(refund)
    return refund_view(refund)


def refund_view(refund: Refund) -> dict:
    eta = {
        "awaiting_return": "Refund is issued once the returned item is received and inspected.",
        "processing": "Card refunds usually arrive within 3-5 business days.",
        "completed": "Refund completed.",
    }[refund.status]
    return {
        "refund_id": refund.id,
        "order_id": refund.order_id,
        "amount": money(refund.amount),
        "method": "account balance" if refund.method == "balance" else "original card",
        "source": refund.source,
        "status": refund.status,
        "created_at": fmt_time(refund.created_at),
        "completed_at": fmt_time(refund.completed_at),
        "note": eta,
    }


# --- Address change -------------------------------------------------------


def check_address_change(session: Session, customer_id: int, order_id: str) -> Order:
    order = get_order(session, customer_id, order_id)
    if order.status not in OrderStatus.PRE_SHIPMENT:
        raise RuleViolation(
            f"Order {order.id} is '{STATUS_LABELS[order.status]}'. The shipping address can "
            "only be changed before the order ships."
        )
    return order


def change_address(session: Session, customer_id: int, order_id: str, new_address: str,
                   recipient_name: str | None = None,
                   recipient_phone: str | None = None) -> dict:
    order = check_address_change(session, customer_id, order_id)
    if len(new_address.strip()) < 10:
        raise RuleViolation("The new address looks incomplete. Please include street, city and ZIP code.")
    old = order.shipping_address
    order.shipping_address = new_address.strip()
    if recipient_name:
        order.recipient_name = recipient_name.strip()
    if recipient_phone:
        order.recipient_phone = normalize_phone(recipient_phone)
    return {
        "order_id": order.id,
        "old_address": old,
        "new_address": order.shipping_address,
        "recipient": f"{order.recipient_name} ({order.recipient_phone})",
    }
