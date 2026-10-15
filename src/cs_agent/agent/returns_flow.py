"""Guided return & refund flow: an explicit slot-filling state machine.

Slots are filled in a fixed order (order -> items -> reason -> refund method).
Each turn, the customer's reply is parsed by a small extractor (which may fill
several slots at once), the draft is validated against business rules, and the
next missing slot is asked for. When everything is filled, `returns_confirm`
shows a confirmation dialog via `interrupt()` and creates the return.
"""

from decimal import Decimal

from langchain_core.messages import AIMessage
from langgraph.graph import END
from langgraph.types import Command

from cs_agent.agent.state import AgentDeps, CSState, last_human_text
from cs_agent.agent.tools import confirm
from cs_agent.db.session import session_scope
from cs_agent.services import returns
from cs_agent.services.common import BusinessError, fmt_time, money, normalize_order_id


def _ask(draft: dict, awaiting: str, options: list[dict], lines: list[str]) -> Command:
    draft.update(awaiting=awaiting, options=options)
    return Command(goto=END, update={"messages": [AIMessage("\n".join(lines))],
                                     "return_draft": draft})


def _exit(text: str) -> Command:
    return Command(goto=END, update={"messages": [AIMessage(text)],
                                     "active_flow": None, "return_draft": None})


def _numbered(options: list[dict]) -> list[str]:
    return [f"{i}. {o['label']}" for i, o in enumerate(options, 1)]


def _apply(draft: dict, slots, notes: list[str]) -> None:
    if slots.order_id:
        try:
            new_order = normalize_order_id(slots.order_id)
        except BusinessError as exc:
            notes.append(str(exc))
        else:
            if new_order != draft.get("order_id"):
                draft["item_ids"] = None  # items belong to the previous order
            draft["order_id"] = new_order
    for key in ("item_ids", "reason", "comment", "refund_method"):
        value = getattr(slots, key)
        if value:
            draft[key] = value


def make_return_nodes(deps: AgentDeps):
    window = deps.settings.return_window_days

    def db():
        return session_scope(deps.session_factory)

    def returns_flow(state: CSState) -> Command:
        draft = dict(state.get("return_draft") or {})
        user_id = state.get("user_id")
        if not user_id:
            return _exit("I need to verify your identity before starting a return.")

        notes: list[str] = []
        if draft.get("started"):
            slots = deps.extract_return_slots(draft.get("awaiting", ""), draft.get("options", []),
                                              last_human_text(state))
            if slots.wants_to_stop:
                return _exit("No problem - I've stopped the return request. "
                             "Is there anything else I can help you with?")
            if slots.off_topic:
                # Leave the form and let the agent handle the new request.
                return Command(goto="agent", update={"active_flow": None, "return_draft": None,
                                                     "needs_knowledge": False})
            _apply(draft, slots, notes)
        else:
            draft["started"] = True

        with db() as s:
            # 1. Order
            order = None
            if draft.get("order_id"):
                try:
                    order = returns.load_returnable_order(s, user_id, draft["order_id"], window)
                except BusinessError as exc:
                    notes.append(str(exc))
                    draft["order_id"] = draft["item_ids"] = None
            if order is None:
                eligible = returns.eligible_orders(s, user_id, window)
                if not eligible:
                    return _exit(" ".join(notes + [
                        f"None of your orders can be returned right now: returns are accepted "
                        f"within {window} days of delivery. If an item is faulty, it may still "
                        "be covered by the warranty, and I can book a repair for you."]))
                if len(eligible) == 1 and not notes:
                    order = eligible[0]
                    draft["order_id"] = order.id
                    notes.append(f"Order {order.id} is your only order within the return window, "
                                 "so I've selected it.")
                else:
                    options = [{"order_id": o.id,
                                "label": f"{o.id}: {', '.join(i.product_name for i in o.items)} "
                                         f"(delivered {fmt_time(o.delivered_at)[:10]})"}
                               for o in eligible]
                    return _ask(draft, "which order to return items from", options,
                                notes + ["Which order would you like to return items from?",
                                         *_numbered(options)])

            # Details the customer gave before the form opened ("the charger, it's broken").
            if hint := draft.pop("hint", None):
                options = [{"item_id": i.id, "label": i.product_name} for i in order.items]
                slots = deps.extract_return_slots("details mentioned before the form opened",
                                                  options, hint)
                slots.order_id = None  # the order is settled above
                _apply(draft, slots, notes)

            # 2. Items
            valid = {i.id for i in order.items}
            chosen = [i for i in (draft.get("item_ids") or []) if i in valid]
            if len(order.items) == 1:
                chosen = [order.items[0].id]
            draft["item_ids"] = chosen or None
            if not chosen:
                options = [{"item_id": i.id,
                            "label": f"{i.quantity} x {i.product_name} ({money(i.unit_price)})"}
                           for i in order.items]
                return _ask(draft, "which item(s) to return", options,
                            notes + [f"Which item(s) from {order.id} would you like to return? "
                                     "You can pick more than one.", *_numbered(options)])

            # 3. Reason
            if not draft.get("reason"):
                options = [{"reason": k, "label": v} for k, v in returns.RETURN_REASONS.items()]
                return _ask(draft, "the reason for the return", options,
                            notes + ["What's the reason for the return?", *_numbered(options)])

            # 4. Refund method
            if not draft.get("refund_method"):
                options = [{"refund_method": k, "label": v}
                           for k, v in returns.REFUND_METHODS.items()]
                return _ask(draft, "the refund method", options,
                            notes + ["How would you like to be refunded?", *_numbered(options)])

        draft["awaiting"], draft["options"] = "confirmation", []
        return Command(goto="returns_confirm", update={"return_draft": draft})

    def returns_confirm(state: CSState) -> dict:
        draft = state["return_draft"]
        user_id = state["user_id"]
        done = {"active_flow": None, "return_draft": None}
        try:
            with db() as s:
                order = returns.load_returnable_order(s, user_id, draft["order_id"], window)
                items = [i for i in order.items if i.id in draft["item_ids"]]
                amount = sum((Decimal(i.unit_price) * i.quantity for i in items), Decimal("0"))
        except BusinessError as exc:
            return {**done, "messages": [AIMessage(f"I couldn't submit the return: {exc}")]}

        details = {
            "Order": order.id,
            "Items": ", ".join(f"{i.quantity} x {i.product_name}" for i in items),
            "Reason": returns.RETURN_REASONS[draft["reason"]],
            "Refund": f"{money(amount)} to {returns.REFUND_METHODS[draft['refund_method']].lower()}",
        }
        if draft.get("comment"):
            details["Details"] = draft["comment"]
        if not confirm("create_return", "Submit this return request?", details):
            return {**done, "messages": [AIMessage(
                "Okay, I haven't submitted the return request. Let me know if you change your "
                "mind or if there's anything else I can do.")]}

        try:
            with db() as s:
                result = returns.create_return(s, user_id, draft["order_id"], draft["item_ids"],
                                               draft["reason"], draft.get("comment"),
                                               draft["refund_method"], window)
        except BusinessError as exc:
            return {**done, "messages": [AIMessage(f"I couldn't submit the return: {exc}")]}
        text = (
            f"Your return request **{result['return_id']}** for {', '.join(result['items'])} "
            f"has been submitted.\n\n"
            f"- Refund: {result['refund_amount']} to {result['refund_method'].lower()}\n"
            f"- Next steps: {result['next_steps']}"
        )
        return {**done, "messages": [AIMessage(text)]}

    return returns_flow, returns_confirm
