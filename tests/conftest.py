"""
Shared module-scoped TestClient for all HTTP-based tests.
The APScheduler lifespan can only start/stop once per process,
so all test modules that need an HTTP client share this single fixture.
"""
import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool
from fastapi.testclient import TestClient

from app.main import app
from app.database import get_session
from app.csrf import verify_csrf


@pytest.fixture(name="engine", scope="session")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture(name="client", scope="session")
def client_fixture(engine):
    def override_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[verify_csrf] = lambda: None

    with TestClient(app, follow_redirects=False) as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture(name="db")
def db_fixture(engine):
    with Session(engine) as session:
        yield session
