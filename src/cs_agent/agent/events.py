"""Turns a graph run into a flat stream of UI events (shared by the API and the CLI).

Event types:
  token        {text}                 streamed text from the agent LLM
  message_end  {}                     the current streamed agent message is complete
  message      {text, source}         a complete message from a non-LLM node (FAQ, flows...)
  tool         {name, label}          the agent is calling a tool
  sources      {items}                knowledge-base citations for the answer
  handoff      {ticket}               the conversation was handed to a human
  confirm      {interrupt_id, ...}    the graph is paused waiting for a confirmation
  verified     {user_id, name}        the customer verified their identity in this turn
  done         {}
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from cs_agent.agent.classifiers import INTERNAL_TAG

TOOL_LABELS = {
    "search_knowledge_base": "Searching the knowledge base",
    "send_verification_code": "Sending a verification code",
    "list_my_orders": "Looking up your orders",
    "get_order_details": "Opening the order",
    "track_shipment": "Checking the shipment",
    "get_account_balance": "Checking your balance",
    "get_refund_status": "Checking refunds",
    "list_service_slots": "Finding available times",
    "list_my_appointments": "Looking up your appointments",
    "cancel_order": "Preparing the cancellation",
    "change_shipping_address": "Preparing the address change",
    "book_service_appointment": "Preparing the booking",
    "cancel_service_appointment": "Preparing the cancellation",
    "request_password_reset": "Sending a reset link",
    "start_return_request": "Opening the return form",
}


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


async def stream_turn(graph, payload: Any, config: dict) -> AsyncIterator[Event]:
    """Run one turn (a new message or a resume Command) and yield UI events."""
    before = (await graph.aget_state(config)).values.get("user_id")

    async for mode, chunk in graph.astream(payload, config, stream_mode=["messages", "updates"]):
        if mode == "messages":
            message, meta = chunk
            # AIMessageChunk subclasses AIMessage: this matches streamed chunks and
            # whole messages from non-streaming models alike.
            if (meta.get("langgraph_node") == "agent"
                    and INTERNAL_TAG not in (meta.get("tags") or [])
                    and isinstance(message, AIMessage)):
                text = str(message.text)
                if text:
                    yield Event("token", {"text": text})
            continue

        for node, update in chunk.items():
            if node == "__interrupt__" or not isinstance(update, dict):
                continue
            for message in update.get("messages") or []:
                if isinstance(message, AIMessage):
                    if node == "agent":
                        if message.text:
                            yield Event("message_end")
                        for call in message.tool_calls:
                            yield Event("tool", {"name": call["name"],
                                                 "label": TOOL_LABELS.get(call["name"], call["name"])})
                    elif message.text:
                        source = message.response_metadata.get("source", node)
                        yield Event("message", {"text": str(message.text), "source": source})
                elif isinstance(message, ToolMessage) and isinstance(message.artifact, dict):
                    if sources := message.artifact.get("sources"):
                        yield Event("sources", {"items": sources})
            if update.get("handoff_ticket"):
                yield Event("handoff", {"ticket": update["handoff_ticket"]})

    snapshot = await graph.aget_state(config)
    for pending in snapshot.interrupts:
        yield Event("confirm", {"interrupt_id": pending.id, **pending.value})
    after = snapshot.values.get("user_id")
    if after and after != before:
        yield Event("verified", {"user_id": after, "name": snapshot.values.get("user_name")})
    yield Event("done")
