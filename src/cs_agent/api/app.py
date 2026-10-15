"""FastAPI app: auth endpoints, SSE chat endpoints and the static web UI."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from cs_agent.agent.events import stream_turn
from cs_agent.agent.graph import build_graph
from cs_agent.agent.state import AgentDeps
from cs_agent.auth import otp, tokens
from cs_agent.config import Settings, get_settings
from cs_agent.db.models import Customer
from cs_agent.db.session import session_scope
from cs_agent.services import accounts
from cs_agent.services.common import BusinessError, NotFoundError, mask_phone

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
THREAD_ID = r"^[A-Za-z0-9_-]{8,64}$"


class SendCodeIn(BaseModel):
    phone: str = Field(max_length=30)


class LoginIn(BaseModel):
    phone: str = Field(max_length=30)
    code: str = Field(max_length=10)


class ChatIn(BaseModel):
    thread_id: str = Field(pattern=THREAD_ID)
    message: str = Field(min_length=1, max_length=2000)


class ResumeIn(BaseModel):
    thread_id: str = Field(pattern=THREAD_ID)
    interrupt_id: str
    approved: bool


def _customer_view(customer: Customer) -> dict:
    return {"id": customer.id, "name": customer.name, "phone": mask_phone(customer.phone)}


def create_app(settings: Settings | None = None, deps: AgentDeps | None = None,
               reindex: Callable[[], Any] | None = None) -> FastAPI:
    """Pass `deps` (and optionally `reindex`) to run without real LLMs, e.g. in tests."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if deps is None:
            from cs_agent.runtime import build_runtime

            runtime = await asyncio.to_thread(build_runtime, settings)
            app.state.deps, app.state.reindex = runtime.deps, runtime.reindex
        else:
            app.state.deps, app.state.reindex = deps, reindex
        settings.runtime_dir.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_path)) as saver:
            app.state.graph = build_graph(app.state.deps, saver)
            yield

    app = FastAPI(title="NovaTech Customer Service Agent", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def db(request: Request):
        return session_scope(request.app.state.deps.session_factory)

    def bearer_user(request: Request) -> int | None:
        header = request.headers.get("authorization")
        if not header:
            return None
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(401, "Malformed Authorization header")
        try:
            return tokens.decode_token(settings, token)
        except tokens.InvalidToken:
            raise HTTPException(401, "Your session has expired. Please log in again.") from None

    async def load_thread(request: Request, thread_id: str, token_user: int | None):
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = await request.app.state.graph.aget_state(config)
        owner = snapshot.values.get("user_id")
        if owner and token_user and owner != token_user:
            raise HTTPException(409, "This conversation belongs to another account. "
                                     "Please start a new conversation.")
        return config, snapshot

    def sse(events: AsyncIterator[tuple[str, dict]]) -> StreamingResponse:
        async def body():
            try:
                async for event, data in events:
                    yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            except Exception:
                log.exception("Chat turn failed")
                error = {"message": "Sorry, something went wrong on our side. Please try again."}
                yield f"event: error\ndata: {json.dumps(error)}\n\n"
                yield "event: done\ndata: {}\n\n"

        return StreamingResponse(body(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def run_turn(graph, payload, config, token_user: int | None):
        async for event in stream_turn(graph, payload, config):
            if event.type == "verified":
                # Verified inside the chat: give the browser the same token a login would.
                if token_user is None:
                    token = tokens.issue_token(settings, event.data["user_id"])
                    yield "auth", {"token": token, "customer": {
                        "id": event.data["user_id"], "name": event.data["name"]}}
                continue
            yield event.type, event.data

    # --- Pages & config ------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/config")
    async def config(request: Request):
        result: dict[str, Any] = {"dev_mode": settings.dev_mode}
        if settings.dev_mode:
            with db(request) as s:
                result["demo_customers"] = [{"name": c.name, "phone": c.phone}
                                            for c in s.query(Customer).order_by(Customer.id)]
            result["dev_code"] = settings.dev_otp_code
        return result

    # --- Auth ------------------------------------------------------------------------

    @app.post("/api/auth/send-code")
    async def send_code(body: SendCodeIn, request: Request):
        try:
            with db(request) as s:
                sent = otp.send_code(s, settings, body.phone)
        except BusinessError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"sent_to": sent["sent_to"], "dev_hint": sent.get("dev_hint")}

    @app.post("/api/auth/login")
    async def login(body: LoginIn, request: Request):
        with db(request) as s:
            result = otp.verify_code(s, settings, body.phone, body.code)
            if not result.ok:
                raise HTTPException(401, result.error)
            customer = accounts.get_customer(s, result.customer_id)
            return {"token": tokens.issue_token(settings, customer.id),
                    "customer": _customer_view(customer)}

    @app.get("/api/me")
    async def me(request: Request):
        user_id = bearer_user(request)
        if user_id is None:
            raise HTTPException(401, "Not logged in")
        try:
            with db(request) as s:
                return _customer_view(accounts.get_customer(s, user_id))
        except NotFoundError:
            raise HTTPException(401, "Account not found") from None

    # --- Chat ------------------------------------------------------------------------

    @app.post("/api/chat")
    async def chat(body: ChatIn, request: Request):
        token_user = bearer_user(request)
        config, snapshot = await load_thread(request, body.thread_id, token_user)
        if snapshot.interrupts:
            raise HTTPException(409, "Please confirm or cancel the pending action first.")
        payload: dict[str, Any] = {"messages": [HumanMessage(body.message.strip())]}
        if token_user and not snapshot.values.get("user_id"):
            with db(request) as s:
                customer = accounts.get_customer(s, token_user)
                payload.update(user_id=customer.id, user_name=customer.name, pending_phone=None)
        return sse(run_turn(request.app.state.graph, payload, config, token_user))

    @app.post("/api/resume")
    async def resume(body: ResumeIn, request: Request):
        token_user = bearer_user(request)
        config, snapshot = await load_thread(request, body.thread_id, token_user)
        if body.interrupt_id not in {i.id for i in snapshot.interrupts}:
            raise HTTPException(409, "This confirmation is no longer pending.")
        payload = Command(resume={body.interrupt_id: {"approved": body.approved}})
        return sse(run_turn(request.app.state.graph, payload, config, token_user))

    @app.get("/api/history")
    async def history(thread_id: str, request: Request):
        token_user = bearer_user(request)
        _, snapshot = await load_thread(request, thread_id, token_user)
        values = snapshot.values
        messages, sources = [], None
        for m in values.get("messages", []):
            if isinstance(m, HumanMessage):
                messages.append({"role": "user", "text": str(m.text)})
                sources = None
            elif isinstance(m, ToolMessage) and isinstance(m.artifact, dict):
                sources = m.artifact.get("sources") or sources
            elif isinstance(m, AIMessage) and m.text:
                messages.append({"role": "assistant", "text": str(m.text),
                                 "source": m.response_metadata.get("source"),
                                 "sources": sources or []})
                sources = None
        return {
            "messages": messages,
            "pending": [{"interrupt_id": i.id, **i.value} for i in snapshot.interrupts],
            "user": {"id": values.get("user_id"), "name": values.get("user_name")}
            if values.get("user_id") else None,
        }

    # --- Admin -----------------------------------------------------------------------

    @app.post("/api/admin/reindex")
    async def admin_reindex(request: Request):
        if not settings.dev_mode and (not settings.admin_token or
                                      request.headers.get("x-admin-token") != settings.admin_token):
            raise HTTPException(403, "Admin token required")
        if request.app.state.reindex is None:
            raise HTTPException(503, "Reindexing is not available")
        report = await asyncio.to_thread(request.app.state.reindex)
        return report.as_dict() if hasattr(report, "as_dict") else report

    return app
