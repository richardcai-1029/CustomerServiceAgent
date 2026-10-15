"""Scripted end-to-end conversations against the REAL LLM (needs DASHSCOPE_API_KEY).

Run with: uv run python scripts/e2e_conversations.py [scenario ...]
WARNING: this resets the demo database first, because scenarios change orders.
Confirmation dialogs are approved automatically. Read the transcripts to judge quality.
"""

import asyncio
import sys
import uuid

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from cs_agent.agent.events import stream_turn
from cs_agent.agent.graph import build_graph
from cs_agent.config import get_settings
from cs_agent.runtime import build_runtime, init_database

SCENARIOS: dict[str, list[str]] = {
    "faq": ["Hi! What are your customer service hours?", "Thanks a lot!"],
    "rag": [
        "My Aurora X2 headphones only play sound in the left ear cup. What can I do?",
        "And how long does the battery last with noise cancelling on?",
    ],
    "rag_unknown": ["Does the Pulse Watch S support contactless payments?"],
    "verify_and_track": [
        "Where is my order?",
        "4155550101",
        "123456",
        "The smartwatch one",
    ],
    "cancel": ["I need to cancel an order", "415-555-0103", "123456", "ORD-3003 please"],
    "address": [
        "Can I change the delivery address for my laptop order?",
        "4155550101",
        "123456",
        "Send it to 1 Infinite Loop, Cupertino, CA 95014 instead",
    ],
    "return": [
        "I want to return something",
        "My phone is 4155550102",
        "123456",
        "Just the charger",
        "it stopped working after two days",
        "Put it on my store balance",
    ],
    "repair": [
        "My Pulse Watch S screen keeps flickering, can I book a repair?",
        "4155550103",
        "123456",
        "The earliest slot is fine",
    ],
    "password": ["I forgot my password", "4155550101", "123456"],
    "upset": [
        "This is the third time my laptop order is delayed, I'm really annoyed",
        "This is absolutely ridiculous, you are the worst store ever!!!",
    ],
    "human": ["Can I speak to a real person please?"],
    "chinese": ["你好，你们的退货政策是什么？"],
}


async def run(graph, name: str, turns: list[str]) -> None:
    print(f"\n{'=' * 70}\nSCENARIO: {name}\n{'=' * 70}")
    config = {"configurable": {"thread_id": uuid.uuid4().hex}}
    for text in turns:
        print(f"\nCustomer: {text}")
        payload = {"messages": [HumanMessage(text)]}
        while payload is not None:
            pending, buffer = None, ""
            async for event in stream_turn(graph, payload, config):
                d = event.data
                if event.type == "token":
                    buffer += d["text"]
                elif event.type == "message_end" and buffer:
                    print(f"Nova: {buffer}")
                    buffer = ""
                elif event.type == "message":
                    print(f"Nova [{d['source']}]: {d['text']}")
                elif event.type == "tool":
                    print(f"   · tool: {d['name']}")
                elif event.type == "sources":
                    print("   · sources: " + "; ".join(s["citation"] for s in d["items"]))
                elif event.type == "confirm":
                    pending = d
                elif event.type in ("verified", "handoff"):
                    print(f"   · {event.type}: {d}")
            if buffer:
                print(f"Nova: {buffer}")
            payload = None
            if pending:
                print(f"   · CONFIRM '{pending['title']}' {pending['details']} -> approved")
                payload = Command(resume={pending["interrupt_id"]: {"approved": True}})


async def main(names: list[str]) -> None:
    settings = get_settings()
    runtime = build_runtime(settings)
    init_database(runtime.engine, runtime.session_factory, reset=True)
    graph = build_graph(runtime.deps, InMemorySaver())
    for name in names or SCENARIOS:
        await run(graph, name, SCENARIOS[name])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
