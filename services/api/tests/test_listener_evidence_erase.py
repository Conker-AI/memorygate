"""A listener can erase an event it sent, and everything derived from it goes too.

Pi sends imported chat messages through a listener; when the owner deletes the imported chat,
Pi erases each event. The event is run through the real pipeline first (episode, analyses,
memory, vector intent), then erased. Temporary SQLite; no network, no model.
"""

import json

import pytest
from app.core.db import Base
from app.models.deletion_receipt import DeletionReceipt
from app.models.episode_object import EpisodeObject
from app.models.evidence_object import EvidenceObject
from app.models.evidence_source import EvidenceSource
from app.models.memory import Memory
from app.models.object_link import ObjectLink
from app.models.processing_job import ProcessingJob
from app.models.vector_outbox import VectorOutbox
from app.routes import runtime
from app.services import (
    auth_settings_service,
    deletion_recovery,
    listener_forgetting,
    memory_forgetting,
    runtime_pipeline,
)
from app.services.processing_lease import LeaseLostError, fence_job_lease
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

KEY = "listener-test-key-long-enough"
OTHER_KEY = "other-listener-key-long-enough"
SECRET = "I prefer the blue mug at Quillfeather Cafe"


def texts_everywhere(engine):
    """Every value in every table, to prove the words survive nowhere."""
    found = []
    with engine.connect() as db:
        for table in inspect(engine).get_table_names():
            for row in db.execute(text(f'SELECT * FROM "{table}"')):
                found.extend(str(value) for value in row)
    return " ".join(found)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'erase.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, autoflush=False, autocommit=False)
    for module in (runtime, listener_forgetting, memory_forgetting):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr(auth_settings_service, "_attempt_state", {})
    removed = []
    monkeypatch.setattr(listener_forgetting, "delete_memory_embedding", removed.append)
    # The model's proposal repeats the event's words, as a real one would.
    monkeypatch.setattr(runtime_pipeline, "analyze_evidence", lambda content, source_type: {
        "summary": f"Proposal: {content}", "observations": [content],
        "memory_candidates": [{"text": content}], "model": "test-model", "provider": "test",
    })
    with sessions() as db:
        for key, secret in (("imports", KEY), ("phone", OTHER_KEY)):
            db.add(EvidenceSource(
                source_key=key, source_type="imported_chat", label=key,
                config_json=json.dumps({"agent_id": "owner"}),
                secret_json=json.dumps({"ingest_key": secret}),
            ))
        db.commit()
    app = FastAPI()
    app.include_router(runtime.router)
    with TestClient(app) as client:
        yield client, sessions, engine, removed
    engine.dispose()


def send(http, content=SECRET, source="imports", key=KEY):
    response = http.post(f"/runtime/listeners/{source}", headers={"X-MemoryGate-Listener-Key": key},
                         json={"title": f"ChatGPT: {content}", "content": content, "tags": ["imported", "chatgpt"]})
    return response


def sent(http, **changes):
    response = send(http, **changes)
    assert response.status_code == 200, response.text
    return response.json()["evidence_id"]


def process(sessions, evidence_id):
    with sessions() as db:
        evidence = db.get(EvidenceObject, evidence_id)
        job = db.scalars(select(ProcessingJob).where(ProcessingJob.evidence_id == evidence_id)).one()
        content = json.loads(evidence.normalized_payload_json)["content"]
        return runtime_pipeline.process_evidence(db, evidence, content, job)


def erase(http, evidence_id, source="imports", key=KEY):
    return http.delete(f"/runtime/listeners/{source}/evidence/{evidence_id}",
                       headers={"X-MemoryGate-Listener-Key": key})


def test_erase_removes_the_event_and_everything_derived_from_it(setup):
    http, sessions, engine, removed = setup
    evidence_id = sent(http)
    result = process(sessions, evidence_id)
    assert result["memory_id"] and result["ai_analysis_id"]
    assert f"Proposal: {SECRET}" in texts_everywhere(engine)

    response = erase(http, evidence_id)

    assert response.status_code == 200
    assert response.json() == {"status": "erased", "evidence_id": evidence_id, "memory_ids": [result["memory_id"]]}
    everything = texts_everywhere(engine)
    assert "Quillfeather" not in everything and "blue mug" not in everything
    assert removed == [result["memory_id"]]
    with sessions() as db:
        assert db.get(Memory, result["memory_id"]) is None
        evidence = db.get(EvidenceObject, evidence_id)
        assert evidence.processing_state == "invalidated" and evidence.tags_json == "[]"
        assert not db.scalars(select(ProcessingJob)).all()
        assert not db.scalars(select(ObjectLink)).all()
        episode = db.get(EpisodeObject, result["episode_id"])
        assert (episode.title, episode.summary, episode.tags_json) == ("Forgotten listener event", "", "[]")
        assert db.get(DeletionReceipt, ("owner", "listener_evidence", evidence_id)) is not None
        assert db.get(DeletionReceipt, ("owner", "memory", result["memory_id"])) is not None
        intent = db.scalars(select(VectorOutbox).where(VectorOutbox.object_id == result["memory_id"])).one()
        assert (intent.operation, intent.status) == ("delete", "complete")

    # A repeat is a no-op that still answers erased; Pi's retry needs that.
    again = erase(http, evidence_id)
    assert again.status_code == 200 and again.json()["memory_ids"] == []


def test_a_memory_another_event_supports_stays_and_the_shared_episode_keeps_other_words(setup):
    http, sessions, engine, removed = setup
    first = sent(http)
    first_result = process(sessions, first)
    second = sent(http)  # the same words again: the memory is reused, the episode shared
    second_result = process(sessions, second)
    process(sessions, sent(http, content="My sister works at the harbour office"))
    assert second_result["memory_id"] == first_result["memory_id"]
    assert second_result["episode_id"] == first_result["episode_id"]

    assert erase(http, first).json()["memory_ids"] == []

    with sessions() as db:
        assert db.get(Memory, first_result["memory_id"]) is not None
        support = db.scalars(select(ObjectLink).where(ObjectLink.target_id == first_result["memory_id"])).all()
        assert len(support) == 1
    assert removed == []
    # Erasing the second copy takes the memory; the third event's words stay.
    assert erase(http, second).json()["memory_ids"] == [first_result["memory_id"]]
    everything = texts_everywhere(engine)
    assert "Quillfeather" not in everything
    assert "harbour office" in everything


def test_a_memory_the_owner_wrote_keeps_its_text(setup):
    http, sessions, _, removed = setup
    with sessions() as db:
        db.add(Memory(id="owner-made", agent_id="owner", text=SECRET, summary=SECRET,
                      source_type="stated", confidence="high"))
        db.commit()
    evidence_id = sent(http)
    assert process(sessions, evidence_id)["memory_id"] == "owner-made"

    assert erase(http, evidence_id).json()["memory_ids"] == []

    with sessions() as db:
        assert db.get(Memory, "owner-made").text == SECRET
        assert not db.scalars(select(ObjectLink).where(ObjectLink.target_id == "owner-made")).all()
    assert removed == []


def test_erase_needs_this_listener_key_and_this_listener_event(setup):
    http, sessions, _, _ = setup
    mine = sent(http)
    theirs = sent(http, content="I want tea", source="phone", key=OTHER_KEY)

    assert erase(http, mine, key="wrong-key-but-long-enough").status_code == 401
    assert erase(http, mine, key=OTHER_KEY).status_code == 401
    assert http.delete(f"/runtime/listeners/imports/evidence/{mine}").status_code == 401
    # Another listener's event and one that never existed get the same answer.
    for other in (theirs, "no-such-evidence"):
        response = erase(http, other)
        assert response.status_code == 200
        assert response.json() == {"status": "not_found", "evidence_id": other, "memory_ids": []}
    assert erase(http, mine, source="unknown").status_code == 404
    with sessions() as db:
        assert json.loads(db.get(EvidenceObject, mine).normalized_payload_json)["content"] == SECRET
        assert db.get(EvidenceObject, theirs).processing_state == "pending"
        assert db.get(DeletionReceipt, ("owner", "listener_evidence", theirs)) is None


def test_erase_works_on_a_disabled_listener(setup):
    http, sessions, _, _ = setup
    evidence_id = sent(http)
    with sessions() as db:
        db.scalars(select(EvidenceSource).where(EvidenceSource.source_key == "imports")).one().enabled = False
        db.commit()

    assert erase(http, evidence_id).status_code == 200
    assert send(http).status_code == 404  # sending stays refused


def test_a_worker_holding_the_job_commits_nothing_after_the_erase(setup):
    http, sessions, _, _ = setup
    evidence_id = sent(http)
    with sessions() as db:
        job = db.scalars(select(ProcessingJob)).one()
        job.status, job.lease_owner = "processing", "worker-1"
        db.commit()
        job_id = job.id

    assert erase(http, evidence_id).status_code == 200

    with sessions() as db, pytest.raises(LeaseLostError):
        fence_job_lease(db, job_id, "worker-1")


def test_recovery_replay_erases_a_restored_event(setup, tmp_path):
    http, sessions, engine, removed = setup
    evidence_id = sent(http)
    memory_id = process(sessions, evidence_id)["memory_id"]
    # A restored copy taken before the erase still holds the words.
    restored = create_engine(f"sqlite:///{tmp_path / 'restored.db'}")
    Base.metadata.create_all(restored)
    with engine.connect() as source, restored.begin() as target:
        for table in Base.metadata.sorted_tables:
            rows = [dict(row._mapping) for row in source.execute(table.select())]
            if rows:
                target.execute(table.insert(), rows)
    assert "Quillfeather" in texts_everywhere(restored)
    assert erase(http, evidence_id).status_code == 200
    with sessions() as db:
        receipts = deletion_recovery.evidence(db)
    assert ("owner", "listener_evidence", evidence_id) in receipts

    result = deletion_recovery.replay(receipts, sessionmaker(restored))

    assert {"kind": "memory", "id": memory_id} in result["indexDeletes"]
    assert "Quillfeather" not in texts_everywhere(restored)
    assert removed == [memory_id]  # replay never touches the live index
    restored.dispose()
