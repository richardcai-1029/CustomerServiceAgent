"""Tools exposed to the agent.

Security rules enforced here, not in the prompt:
- The customer id always comes from graph state (`runtime.state`), never from
  model-generated arguments.
- Every state-changing action asks the customer via `interrupt()` and only
  proceeds on an explicit approval.
"""

import json
from collections.abc import Callable
from typing import Literal

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.types import Command, interrupt

from cs_agent.agent.state import AgentDeps
from cs_agent.auth import otp
from cs_agent.db.session import session_scope
from cs_agent.services import accounts, appointments, orders
from cs_agent.services.common import BusinessError

AUTH_REQUIRED = (
    "AUTH_REQUIRED: the customer is not verified. Ask for the phone number on their "
    "account, then call send_verification_code."
)

# Tools that change data; the agent node keeps at most one of these per turn.
CONFIRM_TOOLS = {
    "cancel_order",
    "change_shipping_address",
    "book_service_appointment",
    "cancel_service_appointment",
}

OrderStatusFilter = Literal[
    "pending_payment", "paid", "shipped", "delivered", "cancelled", "return_requested", "refunded"
]


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def confirm(action: str, title: str, details: dict) -> bool:
    """Pause the graph until the customer approves or declines in the UI."""
    answer = interrupt({"type": "confirmation", "action": action, "title": title,
                        "details": details})
    if isinstance(answer, dict):
        return bool(answer.get("approved"))
    return bool(answer)


DECLINED = "The customer declined in the confirmation dialog. Nothing was changed."


def make_tools(deps: AgentDeps) -> list[BaseTool]:
    settings = deps.settings

    def db():
        return session_scope(deps.session_factory)

    def as_customer(runtime: ToolRuntime, action: Callable[[int], str]) -> str:
        """Run `action(user_id)` for the verified customer; report errors as text."""
        user_id = runtime.state.get("user_id")
        if not user_id:
            return AUTH_REQUIRED
        try:
            return action(user_id)
        except BusinessError as exc:
            return f"ERROR: {exc}"

    # --- Knowledge base & verification -------------------------------------

    @tool
    def search_knowledge_base(query: str, runtime: ToolRuntime) -> Command:
        """Search product manuals and store policies (usage, specs, troubleshooting,
        warranty, shipping, returns). Use a specific, self-contained query."""
        passages = deps.retriever.search(query) if deps.retriever else []
        misses = runtime.state.get("unresolved_count", 0)
        if passages:
            content = "\n\n".join(
                f"[{i}] (source: {p.citation})\n{p.text}" for i, p in enumerate(passages, 1)
            )
            misses = 0
        else:
            content = "NO_RESULTS: the knowledge base has nothing relevant to this question."
            misses += 1
        sources = [{"n": i, "citation": p.citation, "title": p.title, "score": p.score}
                   for i, p in enumerate(passages, 1)]
        return Command(update={
            "unresolved_count": misses,
            "messages": [ToolMessage(content, artifact={"sources": sources},
                                     tool_call_id=runtime.tool_call_id)],
        })

    @tool
    def send_verification_code(phone: str, runtime: ToolRuntime) -> Command | str:
        """Send a 6-digit SMS verification code to the phone number on the customer's
        account. The customer then replies with the code to verify their identity."""
        if runtime.state.get("user_id"):
            return "The customer is already verified."
        try:
            with db() as s:
                sent = otp.send_code(s, settings, phone)
        except otp.ResendTooSoon as exc:
            # The earlier code is still valid: wait for it instead of failing.
            return Command(update={
                "pending_phone": exc.phone,
                "messages": [ToolMessage(
                    "A code was already sent to this number less than a minute ago and is "
                    "still valid. Ask the customer to reply with that code.",
                    tool_call_id=runtime.tool_call_id)],
            })
        except BusinessError as exc:
            return f"ERROR: {exc}"
        text = (f"If {sent['sent_to']} is registered, a code was sent. It expires in "
                f"{sent['expires_in_seconds'] // 60} minutes. Ask the customer to reply with it.")
        if "dev_hint" in sent:
            text += f" (Tell the customer: {sent['dev_hint']})"
        return Command(update={
            "pending_phone": sent["phone"],
            "messages": [ToolMessage(text, tool_call_id=runtime.tool_call_id)],
        })

    # --- Read-only account tools -------------------------------------------

    @tool
    def list_my_orders(runtime: ToolRuntime, status: OrderStatusFilter | None = None) -> str:
        """List the verified customer's orders, newest first, optionally filtered by status."""

        def run(user_id: int) -> str:
            with db() as s:
                result = orders.list_orders(s, user_id, status)
            return _json(result) if result else "The customer has no matching orders."

        return as_customer(runtime, run)

    @tool
    def get_order_details(order_id: str, runtime: ToolRuntime) -> str:
        """Get full details of one order: items (with item ids), address, payment, dates."""

        def run(user_id: int) -> str:
            with db() as s:
                return _json(orders.order_details(orders.get_order(s, user_id, order_id)))

        return as_customer(runtime, run)

    @tool
    def track_shipment(order_id: str, runtime: ToolRuntime) -> str:
        """Get the carrier, tracking number and tracking events of an order."""

        def run(user_id: int) -> str:
            with db() as s:
                return _json(orders.tracking(s, user_id, order_id))

        return as_customer(runtime, run)

    @tool
    def get_account_balance(runtime: ToolRuntime) -> str:
        """Get the verified customer's store account balance."""

        def run(user_id: int) -> str:
            with db() as s:
                return _json(accounts.balance(s, user_id))

        return as_customer(runtime, run)

    @tool
    def get_refund_status(runtime: ToolRuntime, order_id: str | None = None) -> str:
        """Get refund records and their progress, for one order or for all orders."""

        def run(user_id: int) -> str:
            with db() as s:
                result = accounts.refunds(s, user_id, order_id)
            return _json(result) if result else "No refunds found."

        return as_customer(runtime, run)

    @tool
    def list_service_slots(service_type: Literal["repair", "installation"]) -> str:
        """List upcoming free time slots for in-store repair or home installation."""
        try:
            with db() as s:
                return _json(appointments.available_slots(s, service_type))
        except BusinessError as exc:
            return f"ERROR: {exc}"

    @tool
    def list_my_appointments(runtime: ToolRuntime) -> str:
        """List the verified customer's service appointments."""

        def run(user_id: int) -> str:
            with db() as s:
                result = appointments.list_appointments(s, user_id)
            return _json(result) if result else "The customer has no appointments."

        return as_customer(runtime, run)

    # --- Actions that require confirmation -----------------------------------

    @tool
    def cancel_order(order_id: str, runtime: ToolRuntime) -> str:
        """Cancel an order that has not shipped yet. Paid orders are refunded automatically."""

        def run(user_id: int) -> str:
            with db() as s:
                order = orders.check_cancellable(s, user_id, order_id)
                summary = orders.order_summary(order)
                details = {"Order": order.id, "Items": ", ".join(summary["items"]),
                           "Total": summary["total"],
                           "Refund": "Automatic, to the original payment method"
                           if order.status == "paid" else "Not needed (unpaid)"}
            if not confirm("cancel_order", f"Cancel order {details['Order']}?", details):
                return DECLINED
            with db() as s:
                return "Order cancelled. " + _json(orders.cancel_order(s, user_id, order_id))

        return as_customer(runtime, run)

    @tool
    def change_shipping_address(order_id: str, new_address: str, runtime: ToolRuntime,
                                recipient_name: str | None = None,
                                recipient_phone: str | None = None) -> str:
        """Change the shipping address (and optionally recipient) of an order that has not
        shipped. new_address must be complete: street, city, state and ZIP code."""

        def run(user_id: int) -> str:
            with db() as s:
                order = orders.check_address_change(s, user_id, order_id)
                details = {"Order": order.id, "Current address": order.shipping_address,
                           "New address": new_address}
                if recipient_name:
                    details["New recipient"] = recipient_name
                if recipient_phone:
                    details["New recipient phone"] = recipient_phone
            if not confirm("change_shipping_address", f"Update the address of {details['Order']}?",
                           details):
                return DECLINED
            with db() as s:
                result = orders.change_address(s, user_id, order_id, new_address,
                                               recipient_name, recipient_phone)
            return "Address updated. " + _json(result)

        return as_customer(runtime, run)

    @tool
    def book_service_appointment(service_type: Literal["repair", "installation"], slot_id: int,
                                 runtime: ToolRuntime, order_id: str | None = None,
                                 notes: str | None = None) -> str:
        """Book a repair or installation appointment in a slot from list_service_slots.
        Include the related order number and a short problem description when known."""

        def run(user_id: int) -> str:
            with db() as s:
                slot = appointments.check_booking(s, user_id, service_type, slot_id, order_id)
                details = {"Service": appointments.SERVICE_TYPES[service_type],
                           "Time": slot.starts_at.strftime("%A %b %d, %H:%M")}
                if order_id:
                    details["Order"] = order_id
                if notes:
                    details["Notes"] = notes
            if not confirm("book_service_appointment", "Book this appointment?", details):
                return DECLINED
            with db() as s:
                result = appointments.book(s, user_id, service_type, slot_id, order_id, notes)
            return "Appointment booked. " + _json(result)

        return as_customer(runtime, run)

    @tool
    def cancel_service_appointment(appointment_id: str, runtime: ToolRuntime) -> str:
        """Cancel one of the customer's booked service appointments."""

        def run(user_id: int) -> str:
            with db() as s:
                appt = appointments.appointment_view(
                    appointments.check_cancellation(s, user_id, appointment_id))
            details = {"Appointment": appt["appointment_id"], "Service": appt["service"],
                       "Time": appt["starts_at"]}
            if not confirm("cancel_service_appointment", f"Cancel appointment {appt['appointment_id']}?",
                           details):
                return DECLINED
            with db() as s:
                return "Appointment cancelled. " + _json(appointments.cancel(s, user_id, appointment_id))

        return as_customer(runtime, run)

    @tool
    def request_password_reset(runtime: ToolRuntime) -> str:
        """Send a password reset link by SMS to the phone number on the verified account.
        The assistant never sees or sets passwords."""

        def run(user_id: int) -> str:
            with db() as s:
                return _json(accounts.request_password_reset(s, user_id))

        return as_customer(runtime, run)

    @tool
    def start_return_request(runtime: ToolRuntime, order_id: str | None = None,
                             item_description: str | None = None,
                             reason: str | None = None) -> Command | str:
        """Open the guided return & refund form. Call it as soon as a verified customer wants
        to return an item or get a refund for a delivered item, even if you don't know the
        order yet: the form lists the eligible orders itself. Pass what the customer already
        said: order number, which item (e.g. "the charger") and the reason, else leave empty."""
        if not runtime.state.get("user_id"):
            return AUTH_REQUIRED
        hint = "; ".join(f"{label}: {value}" for label, value in
                         (("items", item_description), ("reason", reason)) if value)
        draft = {"started": False, "order_id": order_id, "hint": hint or None}
        return Command(update={
            "active_flow": "return",
            "return_draft": draft,
            "messages": [ToolMessage("The guided return form has started and will ask the "
                                     "customer for the remaining details.",
                                     tool_call_id=runtime.tool_call_id)],
        })

    return [
        search_knowledge_base, send_verification_code, list_my_orders, get_order_details,
        track_shipment, get_account_balance, get_refund_status, list_service_slots,
        list_my_appointments, cancel_order, change_shipping_address, book_service_appointment,
        cancel_service_appointment, request_password_reset, start_return_request,
    ]

