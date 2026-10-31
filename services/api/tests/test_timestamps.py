"""A malformed time in a request field is a 422 naming the field, never a 500."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from app.core.agent import get_agent_id
from app.core.db import Base
from app.core.timestamps import parse_timestamp
from app.models.session_transcript import SessionTranscript
from app.routes import transcript as transcript_routes
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


def test_iso_input_parses_exactly_as_before():
    assert parse_timestamp("2026-10-31T09:00:00Z", "x") == datetime(2026, 10, 31, 9, tzinfo=UTC)
    kept = parse_timestamp("2026-10-31T09:00:00+02:00", "x")
    assert kept == datetime(2026, 10, 31, 9, tzinfo=timezone(timedelta(hours=2)))


def test_empty_and_none_mean_no_time():
    assert parse_timestamp(None, "x") is None
    assert parse_timestamp("", "x") is None


def test_naive_text_stays_naive():
    assert parse_timestamp("2026-10-31T09:00:00", "x") == datetime(2026, 10, 31, 9)


def test_bad_text_is_a_422_naming_the_field():
    with pytest.raises(HTTPException) as raised:
        parse_timestamp("yesterday", "session_start")
    assert raised.value.status_code == 422
    assert "session_start" in raised.value.detail


def test_transcript_route_rejects_a_bad_time_and_keeps_a_good_one(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite:///" + str(tmp_path / "timestamps.db"), connect_args=dict(check_same_thread=False)
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(transcript_routes, "SessionLocal", Session)
    app = FastAPI()
    app.include_router(transcript_routes.router)
    app.dependency_overrides[get_agent_id] = lambda: "default"
    client = TestClient(app)

    body = dict(session_id="s1", transcript="hello world", session_start="yesterday")
    response = client.post("/transcripts", json=body)
    assert response.status_code == 422
    assert "session_start" in response.json()["detail"]
    with Session() as db:
        assert db.execute(select(SessionTranscript)).scalars().all() == []

    good = dict(body, session_start="2026-10-31T09:00:00Z", session_end="2026-10-31T10:00:00Z")
    response = client.post("/transcripts", json=good)
    assert 200 <= response.status_code < 300
    # SQLite stores the datetime without its offset, so the read-back is naive.
    assert response.json()["transcript"]["session_start"] == "2026-10-31T09:00:00"
    with Session() as db:
        stored = db.execute(select(SessionTranscript)).scalars().all()
    assert len(stored) == 1
    engine.dispose()
