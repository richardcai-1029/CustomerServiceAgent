"""Demo seed data: 3 customers whose orders cover every order status.

All timestamps are relative to "now" so that time-based rules (e.g. the
7-day return window) behave the same whenever the database is reset.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from cs_agent.db.models import (
    Appointment,
    Customer,
    Order,
    OrderItem,
    OrderStatus as S,
    Refund,
    ReturnItem,
    ReturnRequest,
    ServiceSlot,
    TrackingEvent,
)
from cs_agent.services.common import now

CATALOG: dict[str, tuple[str, Decimal]] = {
    "AX2-BLK": ("Aurora X2 Wireless Headphones", Decimal("199.00")),
    "AX2-CASE": ("Aurora X2 Carry Case", Decimal("25.00")),
    "PWS-SLV": ("Pulse Watch S", Decimal("249.00")),
    "NBA14-512": ("NovaBook Air 14 (16GB/512GB)", Decimal("1099.00")),
    "NP11-128": ("NovaPad 11 (128GB)", Decimal("449.00")),
    "HALO-MINI": ("Halo Mini Smart Speaker", Decimal("59.00")),
    "USBC-65W": ("65W USB-C GaN Charger", Decimal("39.00")),
}

CUSTOMERS = [
    # (id, name, phone, email, balance)
    (1, "Alice Chen", "4155550101", "alice@example.com", Decimal("120.50")),
    (2, "Ben Wright", "4155550102", "ben@example.com", Decimal("0.00")),
    (3, "Carla Diaz", "4155550103", "carla@example.com", Decimal("45.00")),
]

ADDRESSES = {
    1: "88 Market St, Apt 5B, San Francisco, CA 94103",
    2: "17 Elm Avenue, Austin, TX 78701",
    3: "302 Harbor Rd, Seattle, WA 98101",
}


def _order(
    session: Session,
    order_id: str,
    customer_id: int,
    status: str,
    items: list[tuple[str, int]],
    *,
    created_days_ago: float,
    payment_method: str = "card",
    shipped_days_ago: float | None = None,
    delivered_days_ago: float | None = None,
    cancelled_days_ago: float | None = None,
) -> Order:
    t0 = now()
    name, phone, _, _ = _customer_fields(customer_id)
    order = Order(
        id=order_id,
        customer_id=customer_id,
        status=status,
        payment_method=payment_method,
        total_amount=sum((CATALOG[sku][1] * qty for sku, qty in items), Decimal("0")),
        recipient_name=name,
        recipient_phone=phone,
        shipping_address=ADDRESSES[customer_id],
        created_at=t0 - timedelta(days=created_days_ago),
        paid_at=None
        if status == S.PENDING_PAYMENT
        else t0 - timedelta(days=created_days_ago, minutes=-5),
        shipped_at=_ago(t0, shipped_days_ago),
        delivered_at=_ago(t0, delivered_days_ago),
        cancelled_at=_ago(t0, cancelled_days_ago),
    )
    if shipped_days_ago is not None:
        order.carrier = "SwiftShip"
        order.tracking_number = f"SS{order_id[-4:]}{customer_id}0042"
    session.add(order)
    for sku, qty in items:
        session.add(
            OrderItem(
                order_id=order_id,
                sku=sku,
                product_name=CATALOG[sku][0],
                quantity=qty,
                unit_price=CATALOG[sku][1],
            )
        )
    if shipped_days_ago is not None:
        _tracking(session, order_id, customer_id, shipped_days_ago, delivered_days_ago)
    return order


def _ago(t0: datetime, days: float | None) -> datetime | None:
    return None if days is None else t0 - timedelta(days=days)


def _customer_fields(customer_id: int) -> tuple[str, str, str, Decimal]:
    for cid, name, phone, email, balance in CUSTOMERS:
        if cid == customer_id:
            return name, phone, email, balance
    raise KeyError(customer_id)


def _tracking(
    session: Session,
    order_id: str,
    customer_id: int,
    shipped_days_ago: float,
    delivered_days_ago: float | None,
) -> None:
    t0 = now()
    city = ADDRESSES[customer_id].split(",")[-2].strip()
    shipped = t0 - timedelta(days=shipped_days_ago)
    events = [
        (shipped, "Oakland, CA", "Package picked up by SwiftShip"),
        (shipped + timedelta(hours=9), "Reno, NV", "Departed regional sort facility"),
    ]
    if delivered_days_ago is None:
        # Still in transit: only add events that are already in the past.
        transit = shipped + timedelta(hours=26)
        if transit < t0:
            events.append((transit, f"{city} hub", "Arrived at destination hub"))
    else:
        delivered = t0 - timedelta(days=delivered_days_ago)
        events += [
            (delivered - timedelta(hours=5), f"{city} hub", "Out for delivery"),
            (delivered, city, "Delivered - left at front door"),
        ]
    for occurred_at, location, description in events:
        session.add(
            TrackingEvent(
                order_id=order_id,
                occurred_at=occurred_at,
                location=location,
                description=description,
            )
        )


def _service_slots(session: Session) -> list[ServiceSlot]:
    """Two slots per day for each service type over the next 7 days."""
    today = now().replace(hour=0, minute=0, second=0, microsecond=0)
    slots = []
    for day in range(1, 8):
        base = today + timedelta(days=day)
        for service_type, hours in (("repair", (10, 14)), ("installation", (9, 15))):
            for hour in hours:
                slot = ServiceSlot(service_type=service_type, starts_at=base + timedelta(hours=hour))
                session.add(slot)
                slots.append(slot)
    session.flush()
    return slots


def seed(session: Session) -> None:
    t0 = now()
    for cid, name, phone, email, balance in CUSTOMERS:
        session.add(
            Customer(
                id=cid,
                name=name,
                phone=phone,
                email=email,
                balance=balance,
                created_at=t0 - timedelta(days=400 - cid * 30),
            )
        )
    session.flush()

    # --- Alice ---
    _order(session, "ORD-1001", 1, S.DELIVERED, [("AX2-BLK", 1), ("AX2-CASE", 1)],
           created_days_ago=6, shipped_days_ago=4, delivered_days_ago=2)
    _order(session, "ORD-1002", 1, S.PAID, [("NBA14-512", 1)], created_days_ago=1)
    _order(session, "ORD-1003", 1, S.SHIPPED, [("PWS-SLV", 1)],
           created_days_ago=3, payment_method="balance", shipped_days_ago=1.5)
    _order(session, "ORD-1004", 1, S.DELIVERED, [("HALO-MINI", 1)],
           created_days_ago=35, shipped_days_ago=33, delivered_days_ago=30)
    _order(session, "ORD-1005", 1, S.REFUNDED, [("USBC-65W", 1)],
           created_days_ago=25, shipped_days_ago=24, delivered_days_ago=22)
    session.flush()
    _completed_return(session, "RMA-A1005", "ORD-1005", 1, t0 - timedelta(days=20))

    # --- Ben ---
    _order(session, "ORD-2001", 2, S.PENDING_PAYMENT, [("NP11-128", 1)], created_days_ago=0.2)
    _order(session, "ORD-2002", 2, S.DELIVERED, [("NP11-128", 1), ("USBC-65W", 1)],
           created_days_ago=9, shipped_days_ago=7, delivered_days_ago=5)
    _order(session, "ORD-2003", 2, S.CANCELLED, [("HALO-MINI", 2)],
           created_days_ago=12, cancelled_days_ago=10)
    _order(session, "ORD-2004", 2, S.RETURN_REQUESTED, [("PWS-SLV", 1)],
           created_days_ago=10, shipped_days_ago=8, delivered_days_ago=6)
    session.flush()
    session.add(Refund(id="RF-B2003", order_id="ORD-2003", customer_id=2, amount=Decimal("118.00"),
                       method="card", source="cancellation", status="completed",
                       created_at=t0 - timedelta(days=10), completed_at=t0 - timedelta(days=7)))
    _pending_return(session, "RMA-B2004", "ORD-2004", 2, t0 - timedelta(days=1))

    # --- Carla ---
    _order(session, "ORD-3001", 3, S.SHIPPED, [("HALO-MINI", 2)],
           created_days_ago=2, shipped_days_ago=1)
    _order(session, "ORD-3002", 3, S.DELIVERED, [("NBA14-512", 1)],
           created_days_ago=5, shipped_days_ago=3, delivered_days_ago=1)
    _order(session, "ORD-3003", 3, S.PAID, [("AX2-BLK", 1)],
           created_days_ago=0.1, payment_method="balance")
    _order(session, "ORD-3004", 3, S.DELIVERED, [("PWS-SLV", 1)],
           created_days_ago=16, shipped_days_ago=14, delivered_days_ago=12)
    session.flush()

    slots = _service_slots(session)
    repair_slot = next(s for s in slots if s.service_type == "repair")
    repair_slot.is_booked = True
    session.add(Appointment(id="APT-C3004", customer_id=3, slot_id=repair_slot.id,
                            service_type="repair", order_id="ORD-3004",
                            notes="Watch screen flickers after charging", status="booked",
                            created_at=t0 - timedelta(days=1)))


def _completed_return(session: Session, rma: str, order_id: str, customer_id: int,
                      created_at: datetime) -> None:
    item = session.query(OrderItem).filter_by(order_id=order_id).first()
    amount = item.unit_price * item.quantity
    session.add(ReturnRequest(id=rma, order_id=order_id, customer_id=customer_id,
                              reason="defective", comment="Charger stopped working",
                              refund_method="original", refund_amount=amount,
                              status="completed", created_at=created_at))
    session.flush()
    session.add(ReturnItem(return_id=rma, order_item_id=item.id, quantity=item.quantity))
    session.add(Refund(id=f"RF-{rma[4:]}", order_id=order_id, customer_id=customer_id,
                       return_id=rma, amount=amount, method="card", source="return",
                       status="completed", created_at=created_at,
                       completed_at=created_at + timedelta(days=4)))


def _pending_return(session: Session, rma: str, order_id: str, customer_id: int,
                    created_at: datetime) -> None:
    item = session.query(OrderItem).filter_by(order_id=order_id).first()
    amount = item.unit_price * item.quantity
    session.add(ReturnRequest(id=rma, order_id=order_id, customer_id=customer_id,
                              reason="changed_mind", comment=None, refund_method="balance",
                              refund_amount=amount, status="awaiting_item",
                              created_at=created_at))
    session.flush()
    session.add(ReturnItem(return_id=rma, order_item_id=item.id, quantity=item.quantity))
    session.add(Refund(id=f"RF-{rma[4:]}", order_id=order_id, customer_id=customer_id,
                       return_id=rma, amount=amount, method="balance", source="return",
                       status="awaiting_return", created_at=created_at, completed_at=None))
