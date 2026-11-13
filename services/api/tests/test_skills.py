from datetime import UTC, datetime

from app.core import auth
from app.core.db import Base
from app.models.memory import Memory
from app.routes import skills
from app.routes.skills import context_router, router
from app.services.auth_settings_service import create_agent_access_key, set_admin_key
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def make_client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'skills.db'}", connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(skills, "SessionLocal", Session)
    monkeypatch.setattr(auth, "SessionLocal", Session)
    with Session() as db:
        set_admin_key(db, "Admin-key-123!")
        _, read_key = create_agent_access_key(db, "test", "hermes")
    app = FastAPI()
    app.include_router(router)
    app.include_router(context_router)
    return TestClient(app), read_key


def test_skill_crud_and_read_key_context(tmp_path, monkeypatch):
    client, read_key = make_client(tmp_path, monkeypatch)
    headers = {"X-Agent-Id": "hermes"}

    created = client.post(
        "/skills",
        headers=headers,
        json={
            "title": "Use careful approvals",
            "body": "Never invoke this tool without checking the amount.",
            "linked_tools": ["payments.charge"],
            "version": "1.0",
            "active": True,
        },
    )

    assert created.status_code == 200
    skill = created.json()["skill"]
    assert skill["title"] == "Use careful approvals"
    assert skill["linked_tools"] == ["payments.charge"]

    listed = client.get("/skills", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["results"][0]["id"] == skill["id"]

    patched = client.patch(
        f"/skills/{skill['id']}",
        headers=headers,
        json={"version": "1.1", "body": "Updated markdown instructions."},
    )
    assert patched.status_code == 200
    assert patched.json()["skill"]["version"] == "1.1"

    context = client.get(
        "/context/skills?tool=payments.charge",
        headers={"X-Agent-Id": "hermes", "X-MemoryGate-Key": read_key},
    )
    assert context.status_code == 200
    assert context.json()["results"][0]["body"] == "Updated markdown instructions."


def test_inactive_skill_is_hidden_from_context(tmp_path, monkeypatch):
    client, read_key = make_client(tmp_path, monkeypatch)
    headers = {"X-Agent-Id": "hermes"}
    created = client.post(
        "/skills",
        headers=headers,
        json={"title": "Draft", "body": "Hidden", "linked_tools": ["tool"], "active": False},
    ).json()["skill"]

    context = client.get(
        "/context/skills?tool=tool",
        headers={"X-Agent-Id": "hermes", "X-MemoryGate-Key": read_key},
    )

    assert context.status_code == 200
    assert context.json()["results"] == []
    assert client.delete(f"/skills/{created['id']}", headers=headers).status_code == 200


def test_a_skill_too_big_to_deliver_whole_is_refused_not_cut(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    headers = {"X-Agent-Id": "hermes"}
    big = "x" * (skills.MAX_SKILL_BYTES + 1)

    refused = client.post("/skills", headers=headers, json={"title": "Big", "body": big})
    assert refused.status_code == 422
    assert client.get("/skills", headers=headers).json()["results"] == []

    small = client.post("/skills", headers=headers, json={"title": "Small", "body": "short"}).json()["skill"]
    grown = client.patch(f"/skills/{small['id']}", headers=headers, json={"body": big})
    assert grown.status_code == 422
    assert client.get(f"/skills/{small['id']}", headers=headers).json()["body"] == "short"


def test_one_skill_that_does_not_fit_does_not_drop_the_smaller_ones_after_it(tmp_path, monkeypatch):
    client, read_key = make_client(tmp_path, monkeypatch)
    headers = {"X-Agent-Id": "hermes"}
    half = "y" * (skills.MAX_SKILL_BYTES // 2)
    made = [client.post("/skills", headers=headers, json={"title": title, "body": body, "linked_tools": ["pay"]})
            .json()["skill"]["id"] for title, body in (("Small", "check the amount"), ("Half", half), ("Other half", half))]
    # Newest first: Other half, then Half, then Small.
    with skills.SessionLocal() as db:
        for age, skill_id in enumerate(reversed(made)):
            db.get(Memory, skill_id).updated_at = datetime(2026, 1, 10 - age, tzinfo=UTC)
        db.commit()

    context = client.get("/context/skills?tool=pay", headers={**headers, "X-MemoryGate-Key": read_key}).json()

    # One half fits, the other half then does not, and the small one is still delivered.
    titles = [item["title"] for item in context["results"]]
    assert "Small" in titles and len(titles) == 2
    assert context["bytes"] <= skills.MAX_SKILL_BYTES
