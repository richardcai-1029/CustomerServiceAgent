import json
import uuid

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from cs_agent.api.app import create_app
from cs_agent.db.models import Order, OrderStatus
from tests.fakes import call, make_deps


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


@pytest.fixture
def make_client(settings, session_factory):
    clients = []

    def factory(script=None, **kwargs):
        deps, model = make_deps(settings, session_factory, script=script, **kwargs)
        app = create_app(settings, deps, reindex=lambda: {"added": ["x.md"]})
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for c in clients:
        c.__exit__(None, None, None)


def login(client, phone="4155550101") -> dict:
    client.post("/api/auth/send-code", json={"phone": phone}).raise_for_status()
    response = client.post("/api/auth/login", json={"phone": phone, "code": "123456"})
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['token']}"}


def chat(client, thread, message, headers=None):
    response = client.post("/api/chat", json={"thread_id": thread, "message": message},
                           headers=headers or {})
    return response, parse_sse(response.text) if response.status_code == 200 else []


def test_login_and_me(make_client):
    client = make_client()
    headers = login(client)
    assert client.get("/api/me", headers=headers).json()["name"] == "Alice Chen"
    assert client.get("/api/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    bad = client.post("/api/auth/login", json={"phone": "4155550101", "code": "000000"})
    assert bad.status_code == 401


def test_config_lists_demo_accounts_in_dev_mode(make_client):
    cfg = make_client().get("/api/config").json()
    assert cfg["dev_mode"] and len(cfg["demo_customers"]) == 3


def test_faq_turn_streams_a_message(make_client):
    _, events = chat(make_client(), uuid.uuid4().hex, "What are your business hours?")
    kinds = [e for e, _ in events]
    assert kinds[-1] == "done"
    message = next(d for e, d in events if e == "message")
    assert message["source"] == "faq" and "24/7" in message["text"]


def test_confirmation_round_trip(make_client, session_factory):
    client = make_client(script=[
        call("cancel_order", order_id="ORD-1002"),
        AIMessage("Done - ORD-1002 is cancelled and the refund is on its way."),
    ])
    headers = login(client)
    thread = uuid.uuid4().hex
    _, events = chat(client, thread, "Cancel ORD-1002", headers)
    confirm = next(d for e, d in events if e == "confirm")
    assert confirm["action"] == "cancel_order"
    assert ("tool", {"name": "cancel_order", "label": "Preparing the cancellation"}) in events

    # New messages are refused while a confirmation is pending.
    blocked, _ = chat(client, thread, "hello?", headers)
    assert blocked.status_code == 409

    response = client.post("/api/resume", headers=headers, json={
        "thread_id": thread, "interrupt_id": confirm["interrupt_id"], "approved": True})
    events = parse_sse(response.text)
    tokens = "".join(d["text"] for e, d in events if e == "token")
    assert "cancelled" in tokens
    with session_factory() as s:
        assert s.get(Order, "ORD-1002").status == OrderStatus.CANCELLED

    history = client.get(f"/api/history?thread_id={thread}", headers=headers).json()
    assert history["pending"] == [] and history["user"]["name"] == "Alice Chen"
    assert [m["role"] for m in history["messages"]] == ["user", "assistant"]


def test_in_chat_verification_issues_a_token(make_client):
    client = make_client(script=[
        call("send_verification_code", phone="4155550102"),
        AIMessage("I've sent you a code."),
        AIMessage("Thanks Ben, you're verified. How can I help?"),
    ])
    thread = uuid.uuid4().hex
    chat(client, thread, "My number is 415 555 0102")
    _, events = chat(client, thread, "123456")
    auth = next(d for e, d in events if e == "auth")
    assert auth["customer"]["name"] == "Ben Wright"
    me = client.get("/api/me", headers={"Authorization": f"Bearer {auth['token']}"})
    assert me.json()["id"] == 2


def test_thread_cannot_be_reused_by_another_account(make_client):
    client = make_client(script=[AIMessage("Hi Alice!")])
    alice, ben = login(client), login(client, "4155550102")
    thread = uuid.uuid4().hex
    chat(client, thread, "hi there", alice)
    response, _ = chat(client, thread, "show me the orders", ben)
    assert response.status_code == 409
    assert client.get(f"/api/history?thread_id={thread}", headers=ben).status_code == 409


def test_reindex_endpoint(make_client):
    assert make_client().post("/api/admin/reindex").json() == {"added": ["x.md"]}


def test_index_page_is_served(make_client):
    response = make_client().get("/")
    assert response.status_code == 200 and "NovaTech Support" in response.text
