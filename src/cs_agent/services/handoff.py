"""Simulated hand-off to a human agent."""

from sqlalchemy.orm import Session

from cs_agent.db.models import HandoffTicket
from cs_agent.services.common import new_id, now


def create_ticket(session: Session, *, customer_id: int | None, thread_id: str, reason: str,
                  summary: str) -> str:
    ticket = HandoffTicket(id=new_id("TCK"), customer_id=customer_id, thread_id=thread_id,
                           reason=reason, summary=summary, created_at=now())
    session.add(ticket)
    return ticket.id
