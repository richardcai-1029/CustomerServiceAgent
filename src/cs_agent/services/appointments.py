"""Service appointments (repair / home installation)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from cs_agent.db.models import Appointment, ServiceSlot
from cs_agent.services.common import (
    NotFoundError,
    RuleViolation,
    fmt_time,
    new_id,
    normalize_order_id,
    now,
)
from cs_agent.services.orders import get_order

SERVICE_TYPES = {
    "repair": "In-store repair / diagnostics",
    "installation": "Home installation & setup",
}


def _check_type(service_type: str) -> None:
    if service_type not in SERVICE_TYPES:
        raise RuleViolation(f"Unknown service type '{service_type}'. Use one of: {', '.join(SERVICE_TYPES)}.")


def available_slots(session: Session, service_type: str, limit: int = 8) -> list[dict]:
    _check_type(service_type)
    stmt = (
        select(ServiceSlot)
        .where(ServiceSlot.service_type == service_type, ServiceSlot.is_booked.is_(False),
               ServiceSlot.starts_at > now())
        .order_by(ServiceSlot.starts_at)
        .limit(limit)
    )
    return [{"slot_id": s.id, "starts_at": fmt_time(s.starts_at)} for s in session.scalars(stmt)]


def appointment_view(appt: Appointment) -> dict:
    return {
        "appointment_id": appt.id,
        "service": SERVICE_TYPES[appt.service_type],
        "starts_at": fmt_time(appt.slot.starts_at),
        "order_id": appt.order_id,
        "notes": appt.notes,
        "status": appt.status,
    }


def list_appointments(session: Session, customer_id: int) -> list[dict]:
    stmt = (
        select(Appointment)
        .where(Appointment.customer_id == customer_id)
        .order_by(Appointment.created_at.desc())
    )
    return [appointment_view(a) for a in session.scalars(stmt)]


def check_booking(session: Session, customer_id: int, service_type: str, slot_id: int,
                  order_id: str | None) -> ServiceSlot:
    _check_type(service_type)
    slot = session.get(ServiceSlot, slot_id)
    if slot is None or slot.service_type != service_type:
        raise NotFoundError(f"Slot {slot_id} is not a valid {service_type} slot.")
    if slot.is_booked or slot.starts_at <= now():
        raise RuleViolation(f"Slot {slot_id} is no longer available. Please pick another time.")
    if order_id:
        get_order(session, customer_id, order_id)
    return slot


def book(session: Session, customer_id: int, service_type: str, slot_id: int,
         order_id: str | None = None, notes: str | None = None) -> dict:
    slot = check_booking(session, customer_id, service_type, slot_id, order_id)
    slot.is_booked = True
    appt = Appointment(id=new_id("APT"), customer_id=customer_id, slot_id=slot.id,
                       service_type=service_type,
                       order_id=normalize_order_id(order_id) if order_id else None,
                       notes=notes, status="booked", created_at=now())
    appt.slot = slot
    session.add(appt)
    return appointment_view(appt)


def check_cancellation(session: Session, customer_id: int, appointment_id: str) -> Appointment:
    appt = session.get(Appointment, appointment_id.strip().upper())
    if appt is None or appt.customer_id != customer_id:
        raise NotFoundError(f"No appointment {appointment_id} was found on this account.")
    if appt.status != "booked":
        raise RuleViolation(f"Appointment {appt.id} is already {appt.status}.")
    return appt


def cancel(session: Session, customer_id: int, appointment_id: str) -> dict:
    appt = check_cancellation(session, customer_id, appointment_id)
    appt.status = "cancelled"
    appt.slot.is_booked = False
    return appointment_view(appt)
