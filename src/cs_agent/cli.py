"""Command-line entry point: `cs-agent <command>`."""

import argparse
import asyncio
import logging
import uuid

from cs_agent.config import get_settings


def cmd_init_db(args) -> None:
    from cs_agent.db.session import make_engine, make_session_factory
    from cs_agent.runtime import init_database

    settings = get_settings()
    engine = make_engine(settings.database_url)
    seeded = init_database(engine, make_session_factory(engine), reset=args.reset)
    print("Database reset and seeded." if args.reset else
          "Database initialised with demo data." if seeded else "Database already initialised.")


def cmd_ingest(args) -> None:
    from cs_agent.runtime import build_runtime

    report = build_runtime(get_settings(), sync_docs=False).reindex()
    for key, value in report.as_dict().items():
        print(f"{key:>15}: {value}")


def cmd_serve(args) -> None:
    import uvicorn

    uvicorn.run("cs_agent.api.app:create_app", factory=True, host=args.host, port=args.port,
                reload=args.reload)


def cmd_chat(args) -> None:
    asyncio.run(_chat())


async def _chat() -> None:
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.types import Command

    from cs_agent.agent.events import stream_turn
    from cs_agent.agent.graph import build_graph
    from cs_agent.runtime import build_runtime

    settings = get_settings()
    runtime = await asyncio.to_thread(build_runtime, settings)
    async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_path)) as saver:
        graph = build_graph(runtime.deps, saver)
        config = {"configurable": {"thread_id": uuid.uuid4().hex}}
        print("NovaTech Support (terminal). Commands: /new, /quit")
        while True:
            try:
                text = (await asyncio.to_thread(input, "\nYou: ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if text in ("/quit", "/exit"):
                break
            if text == "/new":
                config = {"configurable": {"thread_id": uuid.uuid4().hex}}
                print("(new conversation)")
                continue
            if not text:
                continue

            payload = {"messages": [HumanMessage(text)]}
            while payload is not None:
                pending, streaming = None, False
                async for event in stream_turn(graph, payload, config):
                    data = event.data
                    if event.type == "token":
                        if not streaming:
                            print("\nNova: ", end="")
                            streaming = True
                        print(data["text"], end="", flush=True)
                    elif event.type == "message_end":
                        print()
                        streaming = False
                    elif event.type == "message":
                        print(f"\nNova: {data['text']}")
                    elif event.type == "tool":
                        print(f"  [{data['label']}...]")
                    elif event.type == "sources":
                        print("  [sources: " + "; ".join(f"[{s['n']}] {s['citation']}"
                                                         for s in data["items"]) + "]")
                    elif event.type == "confirm":
                        pending = data
                    elif event.type == "verified":
                        print(f"  [verified as {data['name']}]")
                if streaming:
                    print()
                payload = None
                if pending:
                    print(f"\n>>> {pending['title']}")
                    for key, value in pending["details"].items():
                        print(f"    {key}: {value}")
                    answer = await asyncio.to_thread(input, "Confirm? [y/N] ")
                    approved = answer.strip().lower().startswith("y")
                    payload = Command(resume={pending["interrupt_id"]: {"approved": approved}})


def main() -> None:
    parser = argparse.ArgumentParser(prog="cs-agent", description="NovaTech customer service agent")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("init-db", help="create tables and seed demo data")
    p.set_defaults(func=cmd_init_db, reset=False)
    p = sub.add_parser("reset-db", help="drop all tables and re-seed demo data")
    p.set_defaults(func=cmd_init_db, reset=True)
    p = sub.add_parser("ingest", help="sync data/docs into the vector store (incremental)")
    p.set_defaults(func=cmd_ingest)
    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)
    p = sub.add_parser("chat", help="chat in the terminal")
    p.set_defaults(func=cmd_chat)

    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.func is cmd_chat and not args.verbose:
        logging.getLogger().setLevel(logging.WARNING)
    args.func(args)


if __name__ == "__main__":
    main()
