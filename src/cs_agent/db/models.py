"""SQLAlchemy ORM models for the mock e-commerce backend."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

Money = Numeric(10, 2)


class Base(DeclarativeBase):
    pass


class OrderStatus:
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    RETURN_REQUESTED = "return_requested"
    REFUNDED = "refunded"

    # Orders in these states have not left the warehouse yet.
    PRE_SHIPMENT = (PENDING_PAYMENT, PAID)


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(200))
    balance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    created_at: Mapped[datetime]

    orders: Mapped[list["Order"]] = relationship(back_populates="customer")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    status: Mapped[str] = mapped_column(String(30))
    payment_method: Mapped[str] = mapped_column(String(20))  # "card" | "balance"
    total_amount: Mapped[Decimal] = mapped_column(Money)
    recipient_name: Mapped[str] = mapped_column(String(100))
    recipient_phone: Mapped[str] = mapped_column(String(20))
    shipping_address: Mapped[str] = mapped_column(String(300))
    carrier: Mapped[str | None] = mapped_column(String(50))
    tracking_number: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime]
    paid_at: Mapped[datetime | None]
    shipped_at: Mapped[datetime | None]
    delivered_at: Mapped[datetime | None]
    cancelled_at: Mapped[datetime | None]

    customer: Mapped[Customer] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", order_by="OrderItem.id"
    )
    tracking_events: Mapped[list["TrackingEvent"]] = relationship(
        back_populates="order", order_by="TrackingEvent.occurred_at"
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    sku: Mapped[str] = mapped_column(String(40))
    product_name: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[int]
    unit_price: Mapped[Decimal] = mapped_column(Money)

    order: Mapped[Order] = relationship(back_populates="items")


class TrackingEvent(Base):
    __tablename__ = "tracking_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    occurred_at: Mapped[datetime]
    location: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(300))

    order: Mapped[Order] = relationship(back_populates="tracking_events")


class ReturnRequest(Base):
    __tablename__ = "return_requests"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    reason: Mapped[str] = mapped_column(String(50))
    comment: Mapped[str | None] = mapped_column(Text)
    refund_method: Mapped[str] = mapped_column(String(20))  # "original" | "balance"
    refund_amount: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(30))  # "awaiting_item" | ...
    created_at: Mapped[datetime]

    items: Mapped[list["ReturnItem"]] = relationship(back_populates="return_request")


class ReturnItem(Base):
    __tablename__ = "return_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    return_id: Mapped[str] = mapped_column(ForeignKey("return_requests.id"), index=True)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_items.id"))
    quantity: Mapped[int]

    return_request: Mapped[ReturnRequest] = relationship(back_populates="items")
    order_item: Mapped[OrderItem] = relationship()


class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    return_id: Mapped[str | None] = mapped_column(ForeignKey("return_requests.id"))
    amount: Mapped[Decimal] = mapped_column(Money)
    method: Mapped[str] = mapped_column(String(20))  # "card" | "balance"
    source: Mapped[str] = mapped_column(String(20))  # "cancellation" | "return"
    # "awaiting_return" -> "processing" -> "completed"
    status: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]


class ServiceSlot(Base):
    __tablename__ = "service_slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_type: Mapped[str] = mapped_column(String(20), index=True)  # "repair" | "installation"
    starts_at: Mapped[datetime]
    is_booked: Mapped[bool] = mapped_column(default=False)


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("service_slots.id"))
    service_type: Mapped[str] = mapped_column(String(20))
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))  # "booked" | "cancelled"
    created_at: Mapped[datetime]

    slot: Mapped[ServiceSlot] = relationship()


class VerificationCode(Base):
    __tablename__ = "verification_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    attempts: Mapped[int] = mapped_column(default=0)
    consumed: Mapped[bool] = mapped_column(default=False)


class PasswordResetRequest(Base):
    __tablename__ = "password_reset_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    sent_to: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime]


class HandoffTicket(Base):
    __tablename__ = "handoff_tickets"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    thread_id: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(50))
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]
