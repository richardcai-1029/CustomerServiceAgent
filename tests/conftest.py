import pytest

from cs_agent.config import Settings
from cs_agent.db.seed import seed
from cs_agent.db.session import create_schema, make_engine, make_session_factory, session_scope


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite://:memory:",
        runtime_dir=tmp_path / "runtime",
        dashscope_api_key="test-key",
        rerank_enabled=False,
        dev_mode=True,
    )


@pytest.fixture
def session_factory(settings):
    engine = make_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = make_session_factory(engine)
    with session_scope(factory) as s:
        seed(s)
    yield factory
    engine.dispose()


@pytest.fixture
def db(session_factory):
    with session_scope(session_factory) as s:
        yield s
