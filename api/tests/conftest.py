"""Test fixtures.

Database tests run against a real Postgres (with pgvector), because the features under test
(vector columns, generated tsvector columns, HNSW indexes) don't exist in SQLite. Each test
runs inside a transaction that is rolled back afterwards, so tests never see each other's data.

Locally, start Postgres with `docker compose up -d db`. If it isn't reachable, database tests
are skipped; CI sets REQUIRE_DB=1 so they fail instead.
"""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.db import get_db, get_session_factory
from app.main import create_app
from app.routes.conversations import get_answerer, get_query_embedder
from app.routes.repos import get_enqueue_index
from tests.fakes import FakeAnswerer, FakeEmbedder

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://devassist:devassist@localhost:5432/devassist_test"
)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    url = make_url(TEST_DATABASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
            )
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    except OperationalError as exc:
        if os.environ.get("REQUIRE_DB"):
            raise
        pytest.skip(f"Postgres not reachable ({exc.orig}); run `docker compose up -d db`")
    finally:
        admin.dispose()

    # Build the schema with the real migrations, down then up, so they are tested too.
    config = Config("alembic.ini")
    config.attributes["database_url"] = url.render_as_string(hide_password=False)
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    engine = create_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as conn:
        outer = conn.begin()
        yield conn
        outer.rollback()


@pytest.fixture
def session_factory(connection: Connection) -> sessionmaker[Session]:
    # Every session joins the test's transaction; a session's commit() only releases a
    # savepoint, and the whole test is rolled back at the end.
    return sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


@pytest.fixture
def db(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@pytest.fixture
def enqueued() -> list[int]:
    """Repo ids the API asked to index, instead of sending them to Celery."""
    return []


@pytest.fixture
def answerer() -> FakeAnswerer:
    return FakeAnswerer()


@pytest.fixture
def client(
    db: Session,
    session_factory: sessionmaker[Session],
    enqueued: list[int],
    answerer: FakeAnswerer,
) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_enqueue_index] = lambda: enqueued.append
    app.dependency_overrides[get_answerer] = lambda: answerer
    app.dependency_overrides[get_query_embedder] = FakeEmbedder
    with TestClient(app) as c:
        yield c
