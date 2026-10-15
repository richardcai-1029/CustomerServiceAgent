import uuid

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from cs_agent.agent.classifiers import EmotionResult, ReturnSlots
from cs_agent.agent.graph import build_graph
from cs_agent.db.models import HandoffTicket, Order, OrderStatus, ReturnRequest
from cs_agent.knowledge.retriever import Passage
from tests.fakes import FakeRetriever, call, make_deps


class Chat:
    """Drives one conversation thread through the graph."""

    def __init__(self, deps):
        self.graph = build_graph(deps, InMemorySaver())
        self.config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    def send(self, text: str, **extra) -> dict:
        return self.graph.invoke({"messages": [HumanMessage(text)], **extra}, self.config)

    def resume(self, approved: bool) -> dict:
        interrupt = self.pending()[0]
        return self.graph.invoke(Command(resume={interrupt.id: {"approved": approved}}), self.config)

    def pending(self):
        return self.graph.get_state(self.config).interrupts

    @property
    def state(self) -> dict:
        return self.graph.get_state(self.config).values


def reply(state: dict) -> str:
    return str(state["messages"][-1].text)


def test_faq_fast_path_does_not_call_the_llm(settings, session_factory):
    deps, model = make_deps(settings, session_factory)
    state = Chat(deps).send("What's your return policy?")
    assert "within 7 days of delivery" in reply(state)
    assert state["messages"][-1].response_metadata["source"] == "faq"
    assert model.calls == []


def test_verification_in_chat_then_continue_request(settings, session_factory):
    deps, model = make_deps(settings, session_factory, script=[
        call("list_my_orders"),
        AIMessage("Sure! What's the phone number on your account?"),
        call("send_verification_code", phone="415-555-0101"),
        AIMessage("I've sent a code to ***-***-0101. Please reply with it."),
        call("list_my_orders"),
        AIMessage("Thanks Alice! Here are your orders..."),
    ])
    chat = Chat(deps)
    chat.send("Where is my order?")
    tool_result = [m for m in chat.state["messages"] if m.type == "tool"][0]
    assert tool_result.text.startswith("AUTH_REQUIRED")

    chat.send("415-555-0101")
    assert chat.state["pending_phone"] == "4155550101"

    state = chat.send("123456")
    assert state["user_id"] == 1 and state["user_name"] == "Alice Chen"
    assert state["pending_phone"] is None
    assert '"order_id": "ORD-1001"' in [m for m in state["messages"] if m.type == "tool"][-1].text
    # The customer's code reply never reached the model or the stored history.
    # (In dev mode the tool result itself reveals the fixed code, by design.)
    human_texts = [str(m.content) for msgs in model.calls for m in msgs if m.type == "human"]
    human_texts += [str(m.content) for m in state["messages"] if m.type == "human"]
    assert all("123456" not in t for t in human_texts)
    assert "[verification code entered - verified successfully]" in human_texts


def test_wrong_code_is_rejected_without_llm(settings, session_factory):
    deps, model = make_deps(settings, session_factory, script=[
        call("send_verification_code", phone="4155550101"),
        AIMessage("Code sent."),
    ])
    chat = Chat(deps)
    chat.send("My phone is 4155550101")
    calls_before = len(model.calls)
    state = chat.send("code is 000000")
    assert "Incorrect code" in reply(state)
    assert not state.get("user_id")
    assert len(model.calls) == calls_before


def test_cancel_order_waits_for_confirmation(settings, session_factory):
    deps, _ = make_deps(settings, session_factory, script=[
        call("cancel_order", order_id="ORD-1002"),
        AIMessage("Your order ORD-1002 has been cancelled and refunded."),
    ])
    chat = Chat(deps)
    chat.send("Please cancel order 1002", user_id=1, user_name="Alice Chen")

    [pending] = chat.pending()
    assert pending.value["action"] == "cancel_order"
    assert pending.value["details"]["Order"] == "ORD-1002"
    with session_factory() as s:
        assert s.get(Order, "ORD-1002").status == OrderStatus.PAID  # nothing happened yet

    chat.resume(approved=True)
    assert not chat.pending()
    with session_factory() as s:
        assert s.get(Order, "ORD-1002").status == OrderStatus.CANCELLED


def test_declined_confirmation_changes_nothing(settings, session_factory):
    deps, _ = make_deps(settings, session_factory, script=[
        call("cancel_order", order_id="ORD-3003"),
        AIMessage("Okay, I've left the order as it is."),
    ])
    chat = Chat(deps)
    chat.send("cancel ORD-3003", user_id=3, user_name="Carla Diaz")
    state = chat.resume(approved=False)
    assert "declined" in [m for m in state["messages"] if m.type == "tool"][-1].text
    with session_factory() as s:
        assert s.get(Order, "ORD-3003").status == OrderStatus.PAID


def test_account_tools_ignore_other_customers_orders(settings, session_factory):
    deps, _ = make_deps(settings, session_factory, script=[
        call("get_order_details", order_id="ORD-1001"),
        AIMessage("I couldn't find that order on your account."),
    ])
    state = Chat(deps).send("details of ORD-1001", user_id=2, user_name="Ben Wright")
    assert "ERROR: No order ORD-1001" in [m for m in state["messages"] if m.type == "tool"][-1].text


def test_guided_return_flow(settings, session_factory):
    with session_factory() as s:
        headphones = next(i.id for i in s.get(Order, "ORD-1001").items if i.sku == "AX2-BLK")
    deps, _ = make_deps(
        settings, session_factory,
        script=[call("start_return_request", reason="they're broken")],
        slots=[
            ReturnSlots(reason="defective", comment="they're broken"),  # initial reason text
            ReturnSlots(item_ids=[headphones]),  # "just the headphones"
            ReturnSlots(refund_method="balance"),  # "store balance please"
        ],
    )
    chat = Chat(deps)
    state = chat.send("I want to return my headphones, they're broken", user_id=1,
                      user_name="Alice Chen")
    # Alice has exactly one returnable order, so it is picked automatically.
    assert "ORD-1001" in reply(state) and "Which item(s)" in reply(state)
    assert state["active_flow"] == "return"

    state = chat.send("just the headphones")
    assert "How would you like to be refunded" in reply(state)

    chat.send("store balance please")
    [pending] = chat.pending()
    assert pending.value["action"] == "create_return"
    assert pending.value["details"]["Refund"] == "$199.00 to store account balance (instant once approved)"

    state = chat.resume(approved=True)
    assert "has been submitted" in reply(state)
    assert state["active_flow"] is None
    with session_factory() as s:
        rma = s.query(ReturnRequest).filter_by(order_id="ORD-1001").one()
        assert rma.reason == "defective" and rma.refund_method == "balance"
    # The extractor saw what was being asked each time.
    assert [c[0] for c in deps.slot_calls] == ["reason", "which item(s) to return",
                                               "the refund method"]


def test_return_flow_can_be_abandoned(settings, session_factory):
    deps, _ = make_deps(
        settings, session_factory,
        script=[call("start_return_request")],
        slots=[ReturnSlots(wants_to_stop=True)],
    )
    chat = Chat(deps)
    chat.send("I'd like to return something", user_id=2, user_name="Ben Wright")
    assert "Which order" not in reply(chat.state)  # Ben has one eligible order (ORD-2002)
    state = chat.send("actually never mind")
    assert "stopped the return" in reply(state) and state["active_flow"] is None


def test_explicit_request_for_human(settings, session_factory):
    deps, model = make_deps(settings, session_factory,
                            emotions=[EmotionResult(wants_human=True)])
    state = Chat(deps).send("Let me talk to a real person")
    assert state["handoff_ticket"].startswith("TCK-")
    assert model.calls == []
    with session_factory() as s:
        assert s.get(HandoffTicket, state["handoff_ticket"]).reason == "customer_request"


def test_repeated_anger_triggers_handoff(settings, session_factory):
    angry = EmotionResult(emotion="angry")
    deps, _ = make_deps(settings, session_factory, emotions=[angry, angry],
                        script=[AIMessage("I'm so sorry. Let me fix this right away.")])
    chat = Chat(deps)
    chat.send("This is ridiculous, my laptop is broken AGAIN!")
    assert not chat.state.get("handoff_ticket")
    state = chat.send("You people are useless!!!")
    assert state["handoff_ticket"] and state["angry_streak"] == 0


def test_repeated_knowledge_misses_trigger_handoff(settings, session_factory):
    deps, _ = make_deps(settings, session_factory, retriever=FakeRetriever([]), script=[
        call("search_knowledge_base", query="Aurora X2 Dolby Atmos"),
        AIMessage("I'm sorry, I couldn't find that in our documentation."),
        call("search_knowledge_base", query="Aurora X2 spatial audio head tracking"),
    ])
    chat = Chat(deps)
    chat.send("Does the Aurora X2 support Dolby Atmos?")
    assert chat.state["unresolved_count"] == 1
    state = chat.send("What about spatial audio with head tracking?")
    assert state["handoff_ticket"]


def test_knowledge_answer_carries_sources(settings, session_factory):
    passage = Passage("[Aurora X2 Headphones Manual - page 4]\nUp to 40 hours with ANC on.",
                      "aurora-x2-headphones-manual.pdf", "Aurora X2 Headphones Manual",
                      "page 4", 0.91)
    deps, model = make_deps(settings, session_factory, retriever=FakeRetriever([passage]), script=[
        call("search_knowledge_base", query="Aurora X2 battery life"),
        AIMessage("Up to 40 hours with noise cancellation on [1]."),
    ])
    state = Chat(deps).send("How long does the Aurora X2 battery last?")
    tool_message = [m for m in state["messages"] if m.type == "tool"][-1]
    assert tool_message.artifact["sources"][0]["citation"] == "aurora-x2-headphones-manual.pdf, page 4"
    assert "[1] (source: aurora-x2-headphones-manual.pdf, page 4)" in tool_message.text
    assert state["unresolved_count"] == 0
