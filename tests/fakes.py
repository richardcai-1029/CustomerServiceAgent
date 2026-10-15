"""Test doubles that let the conversation graph run without any real LLM."""

import json
from collections.abc import Callable, Iterator
from itertools import count

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field

from cs_agent.agent.classifiers import TurnSignals, ReturnSlots
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

    def _next(self, messages) -> AIMessage:
        self.calls.append(list(messages))
        if not self.script:
            raise AssertionError("The scripted model was called more times than expected")
        item = self.script.pop(0)
        return item(messages) if callable(item) else item

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs) -> Iterator[ChatGenerationChunk]:
        """Stream like a real provider: text word by word, tool calls as chunks."""
        message = self._next(messages)
        for i, call in enumerate(message.tool_calls):
            yield ChatGenerationChunk(message=AIMessageChunk("", tool_call_chunks=[{
                "name": call["name"], "args": json.dumps(call["args"]), "id": call["id"],
                "index": i}]))
        words = str(message.content).split(" ")
        for i, word in enumerate(words):
            text = word if i == len(words) - 1 else word + " "
            chunk = ChatGenerationChunk(message=AIMessageChunk(text))
            if run_manager:
                run_manager.on_llm_new_token(text, chunk=chunk)
            yield chunk


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
    signal_queue = list(emotions or [])
    slot_queue = list(slots or [])
    slot_calls: list[tuple] = []

    def classify(messages):
        return signal_queue.pop(0) if signal_queue else TurnSignals()

    def extract(awaiting, options, reply):
        slot_calls.append((awaiting, options, reply))
        return slot_queue.pop(0) if slot_queue else ReturnSlots()

    deps = AgentDeps(
        settings=settings,
        session_factory=session_factory,
        chat_model=model,
        faq=FAQMatcher(load_faq(PROJECT_ROOT / "data" / "faq.yaml")),
        retriever=retriever or FakeRetriever(),
        classify_turn=classify,
        extract_return_slots=extract,
    )
    deps.slot_calls = slot_calls  # exposed for assertions
    return deps, model
