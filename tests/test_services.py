from decimal import Decimal

import pytest

from cs_agent.db.models import Customer, Order, OrderStatus
from cs_agent.services import accounts, appointments, orders, returns
from cs_agent.services.common import NotFoundError, RuleViolation, normalize_order_id

ALICE, BEN, CARLA = 1, 2, 3


def test_order_id_normalization():
    assert normalize_order_id("ord-1001") == "ORD-1001"
    assert normalize_order_id("my order 1001 please") == "ORD-1001"
    with pytest.raises(NotFoundError):
        normalize_order_id("abc")


def test_other_customers_orders_are_hidden(db):
    with pytest.raises(NotFoundError):
        orders.get_order(db, BEN, "ORD-1001")


def test_list_orders_covers_every_status(db):
    statuses = {o["status"] for cid in (ALICE, BEN, CARLA) for o in orders.list_orders(db, cid)}
    assert statuses == {
        OrderStatus.PENDING_PAYMENT, OrderStatus.PAID, OrderStatus.SHIPPED,
        OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.RETURN_REQUESTED,
        OrderStatus.REFUNDED,
    }


def test_tracking_for_shipped_and_unshipped(db):
    shipped = orders.tracking(db, ALICE, "ORD-1003")
    assert shipped["carrier"] == "SwiftShip" and shipped["events"]
    assert "not shipped" in orders.tracking(db, ALICE, "ORD-1002")["message"]


def test_cancel_paid_card_order_creates_processing_refund(db):
    result = orders.cancel_order(db, ALICE, "ORD-1002")
    assert result["status"] == OrderStatus.CANCELLED
    assert result["refund"]["status"] == "processing"
    assert result["refund"]["amount"] == "$1,099.00"


def test_cancel_balance_order_refunds_instantly(db):
    before = db.get(Customer, CARLA).balance
    orders.cancel_order(db, CARLA, "ORD-3003")
    assert db.get(Customer, CARLA).balance == before + Decimal("199.00")


def test_cannot_cancel_shipped_order(db):
    with pytest.raises(RuleViolation):
        orders.cancel_order(db, ALICE, "ORD-1003")


def test_change_address_only_before_shipment(db):
    result = orders.change_address(db, ALICE, "ORD-1002", "1 Infinite Loop, Cupertino, CA 95014")
    assert result["new_address"].startswith("1 Infinite Loop")
    with pytest.raises(RuleViolation):
        orders.change_address(db, ALICE, "ORD-1001", "1 Infinite Loop, Cupertino, CA 95014")


def test_return_window(db):
    eligible = {o.id for o in returns.eligible_orders(db, ALICE, 7)}
    assert eligible == {"ORD-1001"}
    with pytest.raises(RuleViolation, match="return window"):
        returns.load_returnable_order(db, ALICE, "ORD-1004", 7)


def test_partial_return(db):
    order = db.get(Order, "ORD-1001")
    headphones = next(i for i in order.items if i.sku == "AX2-BLK")
    result = returns.create_return(db, ALICE, "ORD-1001", [headphones.id], "defective",
                                   "Left ear cup is silent", "balance", 7)
    assert result["refund_amount"] == "$199.00"
    assert db.get(Order, "ORD-1001").status == OrderStatus.RETURN_REQUESTED
    statuses = [r["status"] for r in accounts.refunds(db, ALICE, "ORD-1001")]
    assert statuses == ["awaiting_return"]


def test_return_rejects_foreign_items(db):
    with pytest.raises(RuleViolation):
        returns.create_return(db, ALICE, "ORD-1001", [999], "defective", None, "original", 7)


def test_appointments_book_and_cancel(db):
    slot = appointments.available_slots(db, "installation")[0]
    appt = appointments.book(db, BEN, "installation", slot["slot_id"], notes="Wall mount")
    assert appt["status"] == "booked"
    assert slot["slot_id"] not in {s["slot_id"] for s in appointments.available_slots(db, "installation")}
    with pytest.raises(RuleViolation):
        appointments.book(db, ALICE, "installation", slot["slot_id"])
    appointments.cancel(db, BEN, appt["appointment_id"])
    with pytest.raises(NotFoundError):
        appointments.cancel(db, ALICE, "APT-C3004")


def test_password_reset_masks_phone(db):
    assert accounts.request_password_reset(db, ALICE)["sent_to"] == "***-***-0101"
