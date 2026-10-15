"""Test doubles that let the conversation graph run without any real LLM."""

from collections.abc import Callable
from itertools import count

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from cs_agent.agent.classifiers import EmotionResult, ReturnSlots
from cs_agent.agent.state import AgentDeps
from cs_agent.knowledge.faq import FAQMatcher, load_faq
from cs_agent.config import PROJECT_ROOT

_ids = count(1)


def call(name: str, **args) -> AIMessage:
    """An AI message that calls one tool."""
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": f"call_{next(_ids)}",
                                      "type": "tool_call"}])


class ScriptedChatModel(BaseChatModel):
    """Returns pre-scripted replies in order and records what it was sent."""

    script: list[AIMessage | Callable[[list[BaseMessage]], AIMessage]] = Field(default_factory=list)
    calls: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls.append(list(messages))
        if not self.script:
            raise AssertionError("The scripted model was called more times than expected")
        item = self.script.pop(0)
        message = item(messages) if callable(item) else item
        return ChatResult(generations=[ChatGeneration(message=message)])


class FakeRetriever:
    def __init__(self, passages=None):
        self.passages = passages or []
        self.queries: list[str] = []

    def search(self, query: str):
        self.queries.append(query)
        return list(self.passages)


def make_deps(settings, session_factory, *, script=None, emotions=None, slots=None,
              retriever=None) -> tuple[AgentDeps, ScriptedChatModel]:
    model = ScriptedChatModel(script=list(script or []))
    emotion_queue = list(emotions or [])
    slot_queue = list(slots or [])
    slot_calls: list[tuple] = []

    def classify(messages):
        return emotion_queue.pop(0) if emotion_queue else EmotionResult()

    def extract(awaiting, options, reply):
        slot_calls.append((awaiting, options, reply))
        return slot_queue.pop(0) if slot_queue else ReturnSlots()

    deps = AgentDeps(
        settings=settings,
        session_factory=session_factory,
        chat_model=model,
        faq=FAQMatcher(load_faq(PROJECT_ROOT / "data" / "faq.yaml")),
        retriever=retriever or FakeRetriever(),
        classify_emotion=classify,
        extract_return_slots=extract,
    )
    deps.slot_calls = slot_calls  # exposed for assertions
    return deps, model
