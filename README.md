# NovaTech Customer Service Agent

A customer service agent built on **LangChain v1 + LangGraph** for NovaTech Store, a fictional consumer-electronics retailer. Conversations, the knowledge base and code comments are in English. Design decisions are recorded in [DESIGN.md](DESIGN.md).

## Features

| Requirement | Implementation |
|---|---|
| Instant FAQ answers | `knowledge/faq.py`: three matching layers (exact → BM25 + token coverage → embeddings); a hit is answered without calling the LLM |
| Multi-turn clarification | Tool schemas + system prompt drive follow-up questions; returns use an explicit slot-filling state machine in `agent/returns_flow.py` |
| Document Q&A (RAG) | `knowledge/ingest.py` incrementally indexes PDF/MD/TXT into Chroma; `retriever.py` does vector recall + DashScope rerank; answers cite their sources. Product questions force a knowledge-base search, so the model cannot answer from memory |
| Small talk & empathy | A per-turn classifier (`qwen-flash`) detects emotion and adapts the tone; repeated anger, an explicit request, or repeated unanswerable questions → hand-off ticket for a human agent |
| Authentication | Simulated SMS codes + JWT; in-chat codes are checked by a deterministic graph node and never reach the LLM context |
| Account queries | Orders, tracking, balance, refund status, appointments (MySQL) |
| Account actions | Cancel orders, change addresses, returns & refunds, book/cancel appointments, password reset; every write action pauses on `interrupt()` for the customer's confirmation |

## Quick start

Prerequisites: Python 3.12, [uv](https://docs.astral.sh/uv/), MySQL (`brew install mysql && brew services start mysql`).

```bash
cp .env.example .env
```

Set `DASHSCOPE_API_KEY` in `.env` (create one in the Alibaba Cloud Model Studio / DashScope console). Then:

```bash
uv sync
```

```bash
mysql -uroot -e "CREATE DATABASE IF NOT EXISTS cs_agent CHARACTER SET utf8mb4"
```

```bash
uv run cs-agent init-db
```

```bash
uv run cs-agent serve
```

Open http://127.0.0.1:8000. On startup the server syncs `data/docs/` into the vector store; only new or changed files are embedded.

Other commands:

| Command | Purpose |
|---|---|
| `uv run cs-agent chat` | Chat in the terminal (answer y/N to confirmations) |
| `uv run cs-agent ingest` | Sync the knowledge base manually (also `POST /api/admin/reindex`) |
| `uv run cs-agent reset-db` | Drop and re-seed the demo data (dates are relative to now) |
| `uv run pytest` | Unit tests (no LLM calls, in-memory SQLite by default) |
| `TEST_DATABASE_URL=mysql+pymysql://root@127.0.0.1:3306/cs_agent_test uv run pytest` | Run the tests against MySQL |
| `uv run python scripts/e2e_conversations.py [scenario ...]` | Scripted conversations with the real LLM (resets the demo database) |

To use DeepSeek for chat, set `LLM_PROVIDER=deepseek` and `DEEPSEEK_API_KEY` in `.env`. Embeddings and rerank still use DashScope.

## Demo accounts

In dev mode the SMS code is always `123456`. Only these seeded phone numbers have accounts; any other number receives a code but cannot sign in.

| Customer | Phone | Good for demonstrating |
|---|---|---|
| Alice Chen | 4155550101 | Returns (ORD-1001 is inside the return window), cancel / change address (ORD-1002 is paid, not shipped), tracking (ORD-1003), a completed refund (ORD-1005) |
| Ben Wright | 4155550102 | An unpaid order, partial returns (ORD-2002), a return in progress (ORD-2004), a cancellation refund |
| Carla Diaz | 4155550103 | Cancelling a balance-paid order refunds instantly (ORD-3003), an existing repair appointment (APT-C3004) |

Things to try:

- "What's your return policy?": FAQ fast path, marked *Instant answer*
- "My Aurora X2 only plays in one ear, what should I do?": RAG answer citing PDF pages
- "Where is my order?": verification → order lookup → asks which order
- "I want to return my headphones": opens the guided return form, ending in a confirmation card
- "I'd like to talk to a person": hand-off to a human with a ticket number

## Project layout

```
src/cs_agent/
  agent/        graph.py (main graph) · tools.py · returns_flow.py · classifiers.py · prompts.py · events.py (SSE events)
  knowledge/    faq.py · ingest.py · retriever.py
  services/     business rules: orders / returns / accounts / appointments / hand-off
  auth/         otp.py (SMS codes) · tokens.py (JWT)
  db/           models.py · seed.py · session.py
  api/          app.py (FastAPI + SSE) · static/ (web UI)
  cli.py · runtime.py · llm.py · config.py
data/           faq.yaml · docs/ (knowledge-base sources) · runtime/ (Chroma, conversation checkpoints; git-ignored)
scripts/        make_sample_pdfs.py · e2e_conversations.py
tests/          services / auth / knowledge / graph / API
```
