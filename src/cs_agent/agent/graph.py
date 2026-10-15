"""The customer-service conversation graph.

    START ─┬─ (awaiting SMS code & reply is a code) ─▶ otp_gate ─▶ agent | END
           ├─ (return form in progress) ─────────────▶ returns_flow
           └─ otherwise ─▶ faq_match ∥ classify ─▶ decide ─┬─▶ handoff
                                                          ├─▶ faq_answer
                                                          └─▶ agent ◀─▶ tools
    tools ─▶ returns_flow (form started) | handoff (repeated misses) | agent
    returns_flow ─▶ returns_confirm (all slots filled) | agent (off-topic) | END
"""

import re
from dataclasses import asdict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, trim_messages
from langgraph.config import get_config
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Checkpointer

from cs_agent.agent.prompts import AGENT_SYSTEM, TONES, today
from cs_agent.agent.returns_flow import make_return_nodes
from cs_agent.agent.state import AgentDeps, CSState, last_human_text
from cs_agent.agent.tools import CONFIRM_TOOLS, make_tools
from cs_agent.auth import otp
from cs_agent.db.session import session_scope
from cs_agent.services import handoff
from cs_agent.services.common import mask_phone

# A reply that is just a 6-digit code, optionally with a few words around it.
OTP_REPLY_RE = re.compile(r"^\D{0,25}(\d{6})\D{0,10}$")
# A reply that is essentially a US phone number ("415-555-0101", "my phone is (415) 555 0101").
PHONE_REPLY_RE = re.compile(r"^\D{0,30}(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\D{0,15}$")
MAX_HISTORY_MESSAGES = 40
# Tools the agent can be forced to call first (via tool_choice) when a turn clearly needs them.
FORCEABLE_TOOLS = ("search_knowledge_base", "send_verification_code")


def forced_tool(state: CSState) -> str | None:
    """Pick a tool the first model call of this turn must make, if any.

    Prompts alone proved unreliable: the model sometimes answered product questions
    from its own (wrong) knowledge, or skipped verification. Forcing the call makes
    grounding and verification structural.
    """
    last = state["messages"][-1]
    if last.type != "human":
        return None  # a later model call within the turn (after a tool ran)
    if not state.get("user_id") and PHONE_REPLY_RE.match(str(last.text).strip()):
        return "send_verification_code"
    if state.get("needs_knowledge"):
        return "search_knowledge_base"
    return None


def build_graph(deps: AgentDeps, checkpointer: Checkpointer = None):
    settings = deps.settings
    tools = make_tools(deps)
    bind_kwargs = {"parallel_tool_calls": False} if settings.llm_provider == "qwen" else {}
    models = {None: deps.chat_model.bind_tools(tools, **bind_kwargs)}
    for name in FORCEABLE_TOOLS:
        models[name] = deps.chat_model.bind_tools(tools, tool_choice=name, **bind_kwargs)
    returns_flow, returns_confirm = make_return_nodes(deps)

    def db():
        return session_scope(deps.session_factory)

    # --- Routing ---------------------------------------------------------------

    def route_start(state: CSState):
        if (state.get("pending_phone") and not state.get("user_id")
                and OTP_REPLY_RE.match(last_human_text(state).strip())):
            return "otp_gate"
        if state.get("active_flow") == "return":
            return "returns_flow"
        return ["faq_match", "classify"]

    def route_decide(state: CSState) -> str:
        if (state.get("wants_human")
                or state.get("angry_streak", 0) >= settings.handoff_angry_streak):
            return "handoff"
        if state.get("faq_hit") and state.get("emotion") != "angry":
            return "faq_answer"
        return "agent"

    def route_agent(state: CSState) -> str:
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else END

    def route_tools(state: CSState) -> str:
        if state.get("active_flow") == "return":
            return "returns_flow"
        if state.get("unresolved_count", 0) >= settings.handoff_unresolved_limit:
            return "handoff"
        return "agent"

    def route_otp(state: CSState) -> str:
        # On success the agent resumes whatever the customer asked before verifying.
        return "agent" if state.get("user_id") else END

    # --- Nodes -----------------------------------------------------------------

    def otp_gate(state: CSState) -> dict:
        message = state["messages"][-1]
        code = OTP_REPLY_RE.match(last_human_text(state).strip()).group(1)
        with db() as s:
            result = otp.verify_code(s, settings, state["pending_phone"], code)
        # Replace the message (same id) so the code never reaches the LLM or the history.
        note = "verified successfully" if result.ok else "verification failed"
        redacted = HumanMessage(f"[verification code entered - {note}]", id=message.id)
        if not result.ok:
            return {"messages": [redacted, AIMessage(result.error)]}
        return {"messages": [redacted], "user_id": result.customer_id,
                "user_name": result.customer_name, "pending_phone": None,
                "needs_knowledge": False}

    def faq_match(state: CSState) -> dict:
        hit = deps.faq.match(last_human_text(state))
        return {"faq_hit": asdict(hit) if hit else None}

    def classify(state: CSState) -> dict:
        signals = deps.classify_turn(state["messages"])
        streak = state.get("angry_streak", 0) + 1 if signals.emotion == "angry" else 0
        return {"emotion": signals.emotion, "wants_human": signals.wants_human,
                "needs_knowledge": signals.needs_knowledge, "angry_streak": streak}

    def decide(state: CSState) -> dict:
        return {}

    def faq_answer(state: CSState) -> dict:
        hit = state["faq_hit"]
        return {"messages": [AIMessage(hit["answer"], response_metadata={
            "source": "faq", "faq_id": hit["faq_id"], "layer": hit["layer"]})]}

    def agent(state: CSState) -> dict:
        if state.get("user_id"):
            auth = f"Verified as {state.get('user_name')}. You may use account tools."
        elif state.get("pending_phone"):
            auth = (f"Not verified yet. A code was sent to {mask_phone(state['pending_phone'])}; "
                    "the customer should reply with it.")
        else:
            auth = ("NOT verified. Account tools will fail until the customer verifies "
                    "(see 'Account requests').")
        system = AGENT_SYSTEM.format(today=today(), auth_status=auth,
                                     tone=TONES.get(state.get("emotion", "calm"), TONES["calm"]))
        history = trim_messages(state["messages"], strategy="last", token_counter=len,
                                max_tokens=MAX_HISTORY_MESSAGES, start_on="human")
        response = models[forced_tool(state)].invoke([SystemMessage(system), *history])
        return {"messages": [_single_confirmation(response)]}

    def handoff_node(state: CSState) -> dict:
        if state.get("wants_human"):
            reason = "customer_request"
        elif state.get("unresolved_count", 0) >= settings.handoff_unresolved_limit:
            reason = "unresolved"
        else:
            reason = "negative_sentiment"
        transcript = "\n".join(f"{m.type}: {str(m.text)}" for m in state["messages"][-8:]
                               if m.type in ("human", "ai") and m.text)
        with db() as s:
            ticket = handoff.create_ticket(s, customer_id=state.get("user_id"),
                                           thread_id=_thread_id(), reason=reason,
                                           summary=transcript[:4000])
        opening = {
            "customer_request": "Of course - I'm connecting you with a human colleague.",
            "unresolved": "I'm sorry I couldn't find the answer. Let me bring in a human colleague.",
            "negative_sentiment": "I'm really sorry about this experience. A human colleague "
                                  "will take it from here.",
        }[reason]
        text = (f"{opening}\n\nYour ticket number is **{ticket}**. A support specialist will "
                "reply in this chat shortly (Mon-Fri 8:00-20:00, Sat-Sun 9:00-18:00 PT). They "
                "will see our conversation, so you won't need to repeat yourself.")
        return {"messages": [AIMessage(text, response_metadata={"source": "handoff"})],
                "handoff_ticket": ticket, "angry_streak": 0, "unresolved_count": 0,
                "wants_human": False}

    # --- Wiring ------------------------------------------------------------------

    builder = StateGraph(CSState)
    builder.add_node("otp_gate", otp_gate)
    builder.add_node("faq_match", faq_match)
    builder.add_node("classify", classify)
    builder.add_node("decide", decide)
    builder.add_node("faq_answer", faq_answer)
    builder.add_node("handoff", handoff_node)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode(tools))
    builder.add_node("returns_flow", returns_flow,
                     destinations=("returns_confirm", "agent", END))
    builder.add_node("returns_confirm", returns_confirm)

    builder.add_conditional_edges(START, route_start,
                                  ["otp_gate", "returns_flow", "faq_match", "classify"])
    builder.add_conditional_edges("otp_gate", route_otp, ["agent", END])
    builder.add_edge(["faq_match", "classify"], "decide")
    builder.add_conditional_edges("decide", route_decide, ["handoff", "faq_answer", "agent"])
    builder.add_conditional_edges("agent", route_agent, ["tools", END])
    builder.add_conditional_edges("tools", route_tools, ["returns_flow", "handoff", "agent"])
    builder.add_edge("faq_answer", END)
    builder.add_edge("handoff", END)
    builder.add_edge("returns_confirm", END)
    return builder.compile(checkpointer=checkpointer)


def _single_confirmation(response: AIMessage) -> AIMessage:
    """Keep at most one tool call when a data-changing tool is involved.

    Some providers ignore `parallel_tool_calls=False`; running two confirmations
    at once would show the customer two dialogs for one reply.
    """
    calls = response.tool_calls
    if len(calls) <= 1 or not any(c["name"] in CONFIRM_TOOLS for c in calls):
        return response
    first = next(c for c in calls if c["name"] in CONFIRM_TOOLS)
    kwargs = {k: v for k, v in response.additional_kwargs.items() if k != "tool_calls"}
    return response.model_copy(update={"tool_calls": [first], "additional_kwargs": kwargs})


def _thread_id() -> str:
    return str(get_config().get("configurable", {}).get("thread_id", "unknown"))
