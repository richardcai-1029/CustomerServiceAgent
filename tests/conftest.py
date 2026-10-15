import os

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


# Set TEST_DATABASE_URL (e.g. mysql+pymysql://root@127.0.0.1:3306/cs_agent_test)
# to run the suite against MySQL instead of in-memory SQLite.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "sqlite:///:memory:")


@pytest.fixture
def session_factory(settings):
    engine = make_engine(TEST_DATABASE_URL)
    create_schema(engine, drop_first=True)
    factory = make_session_factory(engine)
    with session_scope(factory) as s:
        seed(s)
    yield factory
    engine.dispose()


@pytest.fixture
def db(session_factory):
    with session_scope(session_factory) as s:
        yield s
