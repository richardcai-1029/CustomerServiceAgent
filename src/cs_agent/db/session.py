"""Engine and session factory helpers."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from cs_agent.db.models import Base

SessionFactory = sessionmaker[Session]


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite") and ":memory:" in url:
        # A single shared connection so every session sees the same in-memory DB.
        return create_engine(
            url, connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    return create_engine(url, pool_pre_ping=True, pool_recycle=3600)


def make_session_factory(engine: Engine) -> SessionFactory:
    return sessionmaker(engine, expire_on_commit=False)


def create_schema(engine: Engine, *, drop_first: bool = False) -> None:
    if drop_first:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


@contextmanager
def session_scope(factory: SessionFactory) -> Iterator[Session]:
    """Yield a session that commits on success and rolls back on error."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
