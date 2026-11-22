import os
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://tickets:tickets@localhost:5432/tickets")
os.environ.setdefault("AUTO_CREATE_SCHEMA", "true")
os.environ["REDIS_URL"] = ""
from app.config import settings
from app.database import Base
from app.main import app
from app import models  # noqa: F401

@pytest.fixture(scope="session")
def engine():
    eng = create_engine(settings.database_url, pool_pre_ping=True)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()

@pytest.fixture(autouse=True)
def clean_db(engine):
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE'))

@pytest.fixture
def client(engine):
    with TestClient(app) as c:
        yield c

@pytest.fixture
def make_user(client):
    def create(email):
        response = client.post("/auth/register", json={"email": email, "password": "very-secure-password"})
        assert response.status_code == 201, response.text
        return response.json()["access_token"]
    return create

@pytest.fixture
def event_id(engine):
    from datetime import datetime, timezone
    from app.models import Event, Seat
    Session = sessionmaker(engine)
    with Session.begin() as db:
        event = Event(name="Test show", venue="Hall", starts_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
        event.seats = [Seat(label=f"A{i}", price_cents=2500) for i in range(1, 4)]
        db.add(event)
        db.flush()
        return event.id, [seat.id for seat in event.seats]
