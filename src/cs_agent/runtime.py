"""Builds the real (LLM-backed) dependencies shared by the API and the CLI."""

import logging
from dataclasses import dataclass

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from cs_agent.agent.classifiers import make_return_slot_extractor, make_turn_classifier
from cs_agent.agent.state import AgentDeps
from cs_agent.config import Settings
from cs_agent.db.models import Customer
from cs_agent.db.seed import seed
from cs_agent.db.session import (
    SessionFactory,
    create_schema,
    make_engine,
    make_session_factory,
    session_scope,
)
from cs_agent.knowledge.faq import FAQMatcher
from cs_agent.knowledge.ingest import IngestReport, ingest, open_vectorstore
from cs_agent.knowledge.retriever import KnowledgeRetriever, dashscope_reranker
from cs_agent.llm import make_chat_model, make_embeddings

log = logging.getLogger(__name__)


@dataclass
class Runtime:
    settings: Settings
    engine: Engine
    session_factory: SessionFactory
    deps: AgentDeps
    vectorstore: object

    def reindex(self) -> IngestReport:
        return ingest(self.vectorstore, self.settings.docs_dir,
                      self.settings.runtime_dir / "ingest_manifest.json")


def init_database(engine: Engine, factory: SessionFactory, *, reset: bool = False) -> bool:
    """Create tables and seed demo data if needed. Returns True if data was seeded."""
    create_schema(engine, drop_first=reset)
    with session_scope(factory) as s:
        if s.query(Customer).count() > 0:
            return False
        seed(s)
    return True


def build_runtime(settings: Settings, *, sync_docs: bool = True) -> Runtime:
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    if not inspect(engine).has_table("customers"):
        init_database(engine, factory)
        log.info("Database initialised with demo data")

    embeddings = make_embeddings(settings)
    store = open_vectorstore(settings.chroma_dir, embeddings)
    reranker = dashscope_reranker(settings) if settings.rerank_enabled else None
    deps = AgentDeps(
        settings=settings,
        session_factory=factory,
        chat_model=make_chat_model(settings),
        faq=FAQMatcher.from_yaml(settings.faq_path, embeddings,
                                 token_coverage=settings.faq_token_coverage,
                                 embedding_threshold=settings.faq_embedding_threshold),
        retriever=KnowledgeRetriever(store, settings, reranker),
        classify_turn=make_turn_classifier(
            make_chat_model(settings, model=settings.emotion_model_name, temperature=0)),
        extract_return_slots=make_return_slot_extractor(make_chat_model(settings, temperature=0)),
    )
    runtime = Runtime(settings, engine, factory, deps, store)
    if sync_docs:
        # Cheap when nothing changed: only new or edited files are embedded.
        runtime.reindex()
    return runtime
