# Customer Service Agent: Design

This document records the design decisions agreed with the stakeholder before implementation, updated with what changed after testing against the real LLM. Everything in the project is in English: code, comments, knowledge base, UI, agent replies and documentation.

## 1. Scope

- A learning / portfolio project: a clear architecture that is easy to demo. The business backend sits behind a service layer so it can be replaced later.
- Domain: **NovaTech Store**, a fictional consumer-electronics retailer (headphones, smartwatches, laptops, tablets, accessories).
- Language: English only. If a customer writes in another language, the agent still replies in English.

## 2. Tech stack

| Layer | Choice |
|---|---|
| Runtime | Python 3.12, `uv`, `src/cs_agent/` layout |
| Orchestration | LangChain v1 + LangGraph v1 (hand-built `StateGraph`) |
| Main LLM | `qwen-plus` by default (DashScope OpenAI-compatible endpoint); DeepSeek via environment variables |
| Turn classifier | `qwen-flash` (structured output). `qwen-turbo` was the original choice but is rate-limited on new accounts |
| Embeddings / rerank | `text-embedding-v4` / `gte-rerank-v2` (rerank can be switched off) |
| Business database | MySQL (Homebrew), SQLAlchemy 2.x + PyMySQL; unit tests use in-memory SQLite |
| Vector store | Chroma (local, persistent, cosine distance) |
| Conversation memory | LangGraph SQLite checkpointer |
| Interfaces | FastAPI + SSE + a plain HTML/JS single page; plus a CLI |
| Observability | LangSmith, switched on by environment variables only |

## 3. Conversation graph

```
START ─┬─(awaiting code AND reply is a 6-digit code)──▶ otp_gate ──▶ agent / END
       ├─(return form in progress)────────────────────▶ returns_flow
       └─(otherwise)──▶ [faq_match ∥ classify] ──▶ decide
                                                   ├─ handoff      (asked for a human / repeated anger / repeated misses)
                                                   ├─ faq_answer   (FAQ hit and customer not angry)
                                                   └─ agent ◀──▶ tools ──▶ returns_flow (via start_return_request)
returns_flow ──(all slots filled)──▶ returns_confirm (interrupt confirmation) ──▶ END
```

- **otp_gate**: verifies the code deterministically, without the LLM. The customer's message is replaced in the history with `[verification code entered - verified successfully]` (or `- verification failed`), so the code never reaches the model.
- **Layered FAQ matching**: normalized exact match → BM25 shortlist + two-way token coverage → embedding similarity, each with its own threshold. Messages containing order numbers, personal context ("my order") or action requests ("I want to…") skip the FAQ so they reach the agent.
- **Per-turn signals (classify)**: a parallel call to `qwen-flash` returns the emotion (`calm | frustrated | angry`), `wants_human` and `needs_knowledge`. They set the reply tone and trigger the hand-off.
- **Forced tool calls**: when `needs_knowledge` is true, the turn's first model call is forced to `search_knowledge_base` via `tool_choice`. When an unverified customer sends a phone number, it is forced to `send_verification_code`. With prompting alone, the real model sometimes answered product questions from its own (wrong) knowledge and skipped verification.
- **agent**: a tool-calling agent for RAG, account queries and actions, and small talk. Missing details are asked for based on the tool schemas and the system prompt. Tool calls are kept to one per turn when a confirmation is involved (`parallel_tool_calls=False` on Qwen, plus a guard for providers that ignore it), so there is at most one pending confirmation.
- **Return & refund flow**: an explicit slot-filling state machine: order → items → reason → refund method → `interrupt` confirmation → return created. Details the customer gave before the form opened ("the charger, it stopped working") are pre-filled. The flow exits if the customer changes topic or gives up.

## 4. Authentication

- Web sign-in panel: phone number + simulated SMS code (fixed `123456` in dev mode) → JWT (HS256, 24 h).
- In chat: the `send_verification_code` tool sends a code → `otp_gate` checks it → the same JWT is delivered to the browser through an SSE `auth` event.
- Codes are valid for 5 minutes, allow at most 5 attempts, and a number can request a new code at most every 60 seconds while its previous code is still usable. If a resend is throttled, the earlier code stays valid and the conversation waits for it.
- FAQ, RAG and small talk need no verification; everything involving personal data does. Tools read `user_id` only from the conversation state, never from model-generated arguments.
- Once verified, a conversation thread is bound to that customer; requests with another customer's token are rejected. Signing out starts a new thread.
- Account linking (e.g. third-party accounts) is out of scope.

## 5. Business rules (mock data in MySQL)

| Capability | Rule | Confirmation |
|---|---|---|
| Query orders / tracking / balance / refund status / appointments | Read-only | No |
| Cancel order | Only `pending_payment` / `paid`; paid orders are refunded to the original payment method (balance payments refund instantly) | Yes |
| Change shipping address | Only before shipment | Yes |
| Return & refund | Within 7 days of delivery; handled by the return flow; refund to the original payment method or store balance | Yes |
| Book / cancel service appointment | Repair or home installation; simulated time slots | Yes |
| Password reset | Verification required; a reset link is sent to the phone on file (simulated); the agent never handles passwords | No |
| Hand-off to a human | Creates a simulated ticket number | No |

Order statuses: `pending_payment → paid → shipped → delivered`, plus `cancelled`, `return_requested` and `refunded`. Seed data: 3 customers with 4–6 orders each, covering every status. Dates are relative to when the database is seeded; `cs-agent reset-db` re-seeds.

## 6. RAG

- Documents live in `data/docs/`: PDF, Markdown and TXT.
- `cs-agent ingest` or `POST /api/admin/reindex` index incrementally by file SHA-256; deleting a file removes its vectors. The server also syncs on startup.
- Retrieval: vector top-k with a relevance threshold → optional DashScope rerank → top-n passages.
- Answers must cite their sources (file name + page or section), and the UI shows them. If nothing relevant is found, the agent says it couldn't find the information in the documentation (rather than claiming the product lacks the feature) and offers a human.

### Calibrated thresholds

Measured with `text-embedding-v4` and `gte-rerank-v2` on the sample documents:

| Setting | Value | Why |
|---|---|---|
| `rag_min_relevance` | 0.35 | Product questions score ≥ 0.56; off-topic questions ≈ 0.15 |
| `rag_min_rerank_score` | 0.2 | Answerable questions rerank ≥ 0.67; an unanswerable one tops out at 0.19 and should count as a miss |
| `faq_embedding_threshold` | 0.70 | Correct paraphrases score 0.63–0.75 while a wrong match already reaches 0.60; a wrong canned answer is worse than letting the agent answer |

## 7. Testing

- pytest unit tests cover service rules, authentication, FAQ matching, ingestion and the conversation graph. The graph tests use a scripted fake model that streams like a real provider, and cover interrupts, the return flow and hand-offs. No real LLM is called.
- `scripts/e2e_conversations.py` runs scripted conversations against the real LLM; run it manually and read the transcripts.
