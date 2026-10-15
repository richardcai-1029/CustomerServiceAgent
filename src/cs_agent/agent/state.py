"""Conversation state shared by every node of the graph."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from cs_agent.config import Settings
from cs_agent.db.session import SessionFactory
from cs_agent.knowledge.faq import FAQMatcher
from cs_agent.knowledge.retriever import KnowledgeRetriever


class CSState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    # Identity. Only trusted code (the API layer or the OTP gate) sets these;
    # tools read them but never take a user id from the model.
    user_id: NotRequired[int | None]
    user_name: NotRequired[str | None]
    pending_phone: NotRequired[str | None]  # waiting for an SMS code for this phone
    # Per-turn triage results.
    faq_hit: NotRequired[dict | None]
    emotion: NotRequired[str]
    wants_human: NotRequired[bool]
    angry_streak: NotRequired[int]
    unresolved_count: NotRequired[int]  # consecutive knowledge-base misses
    handoff_ticket: NotRequired[str | None]
    # Guided return flow.
    active_flow: NotRequired[str | None]  # "return" while the flow is running
    return_draft: NotRequired[dict | None]


@dataclass
class AgentDeps:
    """Everything the graph needs from the outside world, injectable for tests."""

    settings: Settings
    session_factory: SessionFactory
    chat_model: BaseChatModel
    faq: FAQMatcher
    retriever: KnowledgeRetriever | None
    # messages -> EmotionResult
    classify_emotion: Callable[[list[AnyMessage]], Any]
    # (awaiting, options, reply) -> ReturnSlots
    extract_return_slots: Callable[[str, list[dict], str], Any]


def last_human_text(state: CSState) -> str:
    for message in reversed(state["messages"]):
        if message.type == "human":
            return str(message.text)
    return ""
