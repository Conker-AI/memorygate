"""Forgetting a memory takes the words it came from, unless they still back another memory.

Each event runs through the real listener pipeline (episode, analyses, memory), or through Pi's
conversation admission, and is then forgotten by every owner path: the reviewed forget (one call,
or several in a row as Pi's forget-many sends them) and DELETE /memory/{id}. Afterwards the
evidence is not listed by the explorer, not matched by context retrieval, not analysed again, and
a restore from an older backup replays the same erase. Temporary SQLite; no network, no model.
"""

import json
import shutil

import pytest
from app.core.db import Base
from app.models.analysis_object import AnalysisObject
from app.models.deletion_receipt import DeletionReceipt
from app.models.episode_object import EpisodeObject
from app.models.evidence_object import EvidenceObject
from app.models.evidence_source import EvidenceSource
from app.models.memory import Memory
from app.models.object_link import ObjectLink
from app.models.object_types import OBJECT_MODELS
from app.models.processing_job import ProcessingJob
from app.routes import corrections, runtime
from app.routes import memory as memory_routes
from app.schemas.explorer import LibraryRequest
from app.schemas.runtime import AgentContextRequest
from app.services import (
    auth_settings_service,
    conversation_memory,
    deletion_recovery,
    forgotten_evidence,
    listener_forgetting,
    memory_corrections,
    memory_forgetting,
    processing_worker,
    runtime_pipeline,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

LISTENER_KEY = "listener-test-key-long-enough"
CORRECTION_KEY = "synthetic-correction-key-for-tests-12345"
CORRECTION = {"X-MemoryGate-Correction-Key": CORRECTION_KEY}
OWNER = {"X-Agent-Id": "owner"}
SECRET = "I prefer to keep the spare key under the Wexford planter"
SHARED = "I prefer watering the Marlowe ferns every Sunday"
SPOKEN = "Remember that my locker code is Tamsin-4417"


def texts_everywhere(engine):
    found = []
    with engine.connect() as db:
        for table in inspect(engine).get_table_names():
            for row in db.execute(text(f'SELECT * FROM "{table}"')):
                found.extend(str(value) for value in row)
    return " ".join(found)


def sessions_for(path):
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    return engine, sessionmaker(engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine, sessions = sessions_for(tmp_path / "forget.db")
    assert "transcript" in OBJECT_MODELS  # every explorer table exists in the schema
    Base.metadata.create_all(engine)
    for module in (
        runtime,
        listener_forgetting,
        memory_forgetting,
        memory_corrections,
        memory_routes,
        conversation_memory,
        processing_worker,
    ):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setenv("MEMORYGATE_CORRECTION_KEY", CORRECTION_KEY)
    monkeypatch.setenv("MEMORYGATE_CORRECTION_AGENT_ID", "owner")
    monkeypatch.setattr(auth_settings_service, "_attempt_state", {})
    for module in (memory_forgetting, memory_routes):
        monkeypatch.setattr(module, "delete_memory_embedding", lambda memory_id: None)
    monkeypatch.setattr(
        runtime, "semantic_status", lambda: {"status": "degraded", "reason": "test"}
    )
    # The model's proposal repeats the event's words, as a real one would.
    monkeypatch.setattr(
        runtime_pipeline,
        "analyze_evidence",
        lambda content, source_type: {
            "summary": f"Proposal: {content}",
            "observations": [content],
            "memory_candidates": [{"text": content}],
            "model": "test-model",
            "provider": "test",
        },
    )
    with sessions() as db:
        db.add(
            EvidenceSource(
                source_key="imports",
                source_type="imported_chat",
                label="imports",
                config_json=json.dumps({"agent_id": "owner"}),
                secret_json=json.dumps({"ingest_key": LISTENER_KEY}),
            )
        )
        db.commit()
    app = FastAPI()
    for router in (runtime.router, corrections.router, memory_routes.router):
        app.include_router(router)
    with TestClient(app) as client:
        yield client, sessions, engine, tmp_path
    engine.dispose()


def listened(http, sessions, content):
    response = http.post(
        "/runtime/listeners/imports",
        headers={"X-MemoryGate-Listener-Key": LISTENER_KEY},
        json={"title": f"ChatGPT: {content}", "content": content, "tags": ["imported"]},
    )
    assert response.status_code == 200, response.text
    evidence_id = response.json()["evidence_id"]
    with sessions() as db:
        evidence = db.get(EvidenceObject, evidence_id)
        job = db.scalars(
            select(ProcessingJob).where(ProcessingJob.evidence_id == evidence_id)
        ).one()
        result = runtime_pipeline.process_evidence(db, evidence, content, job)
    assert result["memory_id"]
    return evidence_id, result


def scenario(http, sessions):
    """E1 backs only M. E2 backs M (its analysis also supports M) and M2."""
    first, made = listened(http, sessions, SECRET)
    second, other = listened(http, sessions, SHARED)
    with sessions() as db:
        db.add(
            ObjectLink(
                source_type="analysis",
                source_id=other["analysis_id"],
                target_type="memory",
                target_id=made["memory_id"],
                relationship="supports",
                created_by="runtime",
            )
        )
        db.commit()
    return first, second, made, other


def revision(sessions, memory_id):
    with sessions() as db:
        return db.get(Memory, memory_id).revision


def forget(http, sessions, memory_id, request_id):
    response = http.put(
        f"/runtime/corrections/forget/{request_id}",
        headers=CORRECTION,
        json={"memory_id": memory_id, "expected_revision": revision(sessions, memory_id)},
    )
    assert response.status_code == 200, response.text


def delete(http, sessions, memory_id, request_id):
    response = http.delete(f"/memory/{memory_id}", headers=OWNER)
    assert response.status_code == 200, response.text


PATHS = {"forget": forget, "delete": delete}


def library_ids(search=""):
    found = runtime.memory_library(LibraryRequest(search=search, limit=50), "owner", "admin")
    return {(item["type"], item["id"]) for item in found["objects"]}, json.dumps(found)


def context(query):
    return runtime.agent_context(
        AgentContextRequest(query=query, include_evidence=True), "owner", "admin"
    )


def assert_forgotten(sessions, evidence_id):
    with sessions() as db:
        evidence = db.get(EvidenceObject, evidence_id)
        assert evidence is not None, "the row stays as a content-free tombstone"
        assert evidence.invalidated_at is not None and evidence.processing_state == "invalidated"
        assert (evidence.summary, evidence.normalized_payload_json, evidence.tags_json) == (
            "",
            "{}",
            "[]",
        )
        assert not db.scalars(
            select(ProcessingJob).where(ProcessingJob.evidence_id == evidence_id)
        ).all()
        assert db.get(DeletionReceipt, ("owner", listener_forgetting.KIND, evidence_id)) is not None
    ids, _ = library_ids()
    assert ("evidence", evidence_id) not in ids
    assert evidence_id not in json.dumps(context(SECRET)["evidence"])


@pytest.mark.parametrize("path", sorted(PATHS))
def test_forgetting_a_memory_invalidates_evidence_that_backed_only_it(setup, path):
    http, sessions, engine, _ = setup
    first, second, made, other = scenario(http, sessions)
    assert ("evidence", first) in library_ids()[0]
    assert first in json.dumps(context(SECRET)["evidence"])

    PATHS[path](http, sessions, made["memory_id"], "forget_request_00000001")

    assert_forgotten(sessions, first)
    everything = texts_everywhere(engine)
    assert "Wexford" not in everything and "spare key" not in everything
    assert library_ids("Wexford")[0] == set()
    found = context("Wexford planter spare key")
    assert not found["evidence"] and not found["episodes"] and not found["memories"]
    with sessions() as db:
        assert db.get(Memory, made["memory_id"]) is None
        # The analyses made from E1 alone (with their copies of its words) are gone.
        assert db.get(AnalysisObject, made["analysis_id"]) is None
        assert db.get(AnalysisObject, made["ai_analysis_id"]) is None
        assert not db.scalars(
            select(ObjectLink).where(
                (ObjectLink.source_id == first) | (ObjectLink.target_id == first)
            )
        ).all()
        # E2 still backs M2: it, its analyses, its links and M2 are untouched.
        shared = db.get(EvidenceObject, second)
        assert shared.invalidated_at is None and shared.summary == SHARED
        assert json.loads(shared.normalized_payload_json)["content"] == SHARED
        assert db.get(Memory, other["memory_id"]).text == SHARED
        assert db.get(AnalysisObject, other["analysis_id"]) is not None
        assert db.get(DeletionReceipt, ("owner", listener_forgetting.KIND, second)) is None
        episode = db.get(EpisodeObject, made["episode_id"])
        assert "Wexford" not in episode.title + episode.summary
    assert ("evidence", second) in library_ids("Marlowe")[0]
    assert second in json.dumps(context("Marlowe ferns")["evidence"])


def test_forget_many_invalidates_shared_evidence_once_its_last_memory_goes(setup):
    """Pi's forget-many is one reviewed forget per item, each with its own request id."""
    http, sessions, engine, _ = setup
    first, second, made, other = scenario(http, sessions)

    forget(http, sessions, made["memory_id"], "forget_batch_000001-0")
    with sessions() as db:
        assert db.get(EvidenceObject, second).invalidated_at is None
    forget(http, sessions, other["memory_id"], "forget_batch_000001-1")

    assert_forgotten(sessions, first)
    assert_forgotten(sessions, second)
    assert "Marlowe" not in texts_everywhere(engine)
    assert library_ids("Marlowe")[0] == set()


def test_forgotten_evidence_is_not_analysed_again(setup):
    http, sessions, _, _ = setup
    first, _, made, _ = scenario(http, sessions)
    with sessions() as db:
        job_id = db.scalars(
            select(ProcessingJob.id).where(ProcessingJob.evidence_id == first)
        ).one()
    forget(http, sessions, made["memory_id"], "forget_request_00000002")

    with pytest.raises(Exception) as refused:
        runtime.retry_job(job_id, agent_id="owner")
    assert getattr(refused.value, "status_code", None) == 404
    # Even a job that names the evidence (a stale queue, or one made by hand) promotes nothing.
    with sessions() as db:
        db.add(ProcessingJob(agent_id="owner", evidence_id=first, status="pending", stage="queued"))
        db.commit()
        job = processing_worker.claim_next_job(db, "test-worker")
        assert job is not None and job.evidence_id == first
        processing_worker.run_job(db, job)
        assert db.get(ProcessingJob, job.id).status == "failed"
        assert db.get(EvidenceObject, first).processing_state == "invalidated"
        assert not db.scalars(select(Memory).where(Memory.text.ilike("%Wexford%"))).all()


def test_conversation_evidence_goes_with_its_memory_and_pi_can_still_forget_the_message(setup):
    http, sessions, engine, _ = setup
    receipt = conversation_memory.ingest(
        "owner",
        "msg_locker",
        {
            "session_id": "ses_locker",
            "content": SPOKEN,
            "created_at": 1770000000.0,
        },
    )
    assert receipt["state"] == "admitted"

    delete(http, sessions, receipt["memory_id"], "unused")

    assert_forgotten(sessions, receipt["id"])
    assert "Tamsin" not in texts_everywhere(engine)
    assert conversation_memory.forget("owner", "msg_locker")["state"] == "deleted"
    # Pi retrying the same upload finds the same receipt and makes no new memory.
    conversation_memory.ingest(
        "owner",
        "msg_locker",
        {
            "session_id": "ses_locker",
            "content": SPOKEN,
            "created_at": 1770000000.0,
        },
    )
    with sessions() as db:
        assert not db.scalars(select(Memory)).all()


def test_restoring_an_older_backup_replays_the_evidence_erase(setup):
    # Recovery replays the tombstone through the listener erase, so the kinds must agree.
    assert forgotten_evidence.EVIDENCE_KIND == listener_forgetting.KIND
    http, sessions, engine, tmp_path = setup
    first, second, made, other = scenario(http, sessions)
    engine.dispose()
    shutil.copy(tmp_path / "forget.db", tmp_path / "backup.db")

    forget(http, sessions, made["memory_id"], "forget_request_00000003")

    restored_engine, restored = sessions_for(tmp_path / "backup.db")
    with sessions() as live:
        deletion_recovery.replay(deletion_recovery.evidence(live), restored)
    with restored() as db:
        assert db.get(Memory, made["memory_id"]) is None
        evidence = db.get(EvidenceObject, first)
        assert evidence.invalidated_at is not None and evidence.summary == ""
        assert db.get(EvidenceObject, second).summary == SHARED
        assert db.get(Memory, other["memory_id"]) is not None
    restored_engine.dispose()


OTHER_KEY = "other-listener-test-key-long-enough"
ELSEWHERE = "I prefer the Corvina lamp beside the hall mirror"


def other_agents_event(http, sessions):
    """Agent 'other' hears its own event through the real pipeline."""
    with sessions() as db:
        db.add(
            EvidenceSource(
                source_key="others",
                source_type="imported_chat",
                label="others",
                config_json=json.dumps({"agent_id": "other"}),
                secret_json=json.dumps({"ingest_key": OTHER_KEY}),
            )
        )
        db.commit()
    response = http.post(
        "/runtime/listeners/others",
        headers={"X-MemoryGate-Listener-Key": OTHER_KEY},
        json={"title": f"ChatGPT: {ELSEWHERE}", "content": ELSEWHERE, "tags": ["imported"]},
    )
    assert response.status_code == 200, response.text
    evidence_id = response.json()["evidence_id"]
    with sessions() as db:
        evidence = db.get(EvidenceObject, evidence_id)
        assert evidence.agent_id == "other"
        job = db.scalars(
            select(ProcessingJob).where(ProcessingJob.evidence_id == evidence_id)
        ).one()
        result = runtime_pipeline.process_evidence(db, evidence, ELSEWHERE, job)
    assert result["memory_id"]
    return evidence_id, result


def link(db, source_type, source_id, target_type, target_id, relationship):
    db.add(
        ObjectLink(
            source_type=source_type,
            source_id=source_id,
            target_type=target_type,
            target_id=target_id,
            relationship=relationship,
            created_by="lineage",
        )
    )


@pytest.mark.parametrize("path", sorted(PATHS))
def test_forgetting_never_reaches_another_agents_evidence(setup, path):
    """Lineage links are not checked for namespace (POST /lineage/links), so forgetting walks
    them only inside the forgetting agent's own namespace."""
    http, sessions, engine, _ = setup
    first, made = listened(http, sessions, SECRET)
    theirs, their_result = other_agents_event(http, sessions)
    with sessions() as db:
        # Their evidence joined straight to our memory, and their analysis supporting it.
        link(db, "evidence", theirs, "memory", made["memory_id"], "supports")
        link(db, "analysis", their_result["analysis_id"], "memory", made["memory_id"], "supports")
        # A hand-made analysis of theirs whose only source is our forgotten evidence.
        theirs_alone = AnalysisObject(
            agent_id="other",
            analysis_type="manual",
            evidence_ids_json="[]",
            input_summary="their note",
            output_summary="their note",
        )
        db.add(theirs_alone)
        db.flush()
        link(db, "evidence", first, "analysis", theirs_alone.id, "analysed_in")
        db.commit()
        alone_id = theirs_alone.id
        episode = db.get(EpisodeObject, their_result["episode_id"])
        their_episode = (episode.title, episode.summary, episode.tags_json)

    PATHS[path](http, sessions, made["memory_id"], "forget_request_00000004")

    assert_forgotten(sessions, first)
    with sessions() as db:
        evidence = db.get(EvidenceObject, theirs)
        assert evidence.invalidated_at is None and evidence.summary == ELSEWHERE
        assert json.loads(evidence.normalized_payload_json)["content"] == ELSEWHERE
        assert db.get(DeletionReceipt, ("other", listener_forgetting.KIND, theirs)) is None
        assert db.get(DeletionReceipt, ("owner", listener_forgetting.KIND, theirs)) is None
        for analysis_id in (their_result["analysis_id"], their_result["ai_analysis_id"], alone_id):
            assert db.get(AnalysisObject, analysis_id) is not None
        assert db.get(Memory, their_result["memory_id"]).text == ELSEWHERE
        episode = db.get(EpisodeObject, their_result["episode_id"])
        assert (episode.title, episode.summary, episode.tags_json) == their_episode
    assert ELSEWHERE in texts_everywhere(engine)


@pytest.mark.parametrize("path", sorted(PATHS))
def test_forgetting_never_blanks_another_agents_episode(setup, path):
    """Episodes are grouped without a namespace check (POST /lineage/episodes), so a forget
    blanks only the forgetting agent's own episodes."""
    http, sessions, _engine, _ = setup
    first, made = listened(http, sessions, SECRET)
    _, their_result = other_agents_event(http, sessions)
    with sessions() as db:
        # Our evidence grouped into their episode, which it is the only other member of.
        link(db, "evidence", first, "episode", their_result["episode_id"], "grouped_into")
        db.commit()
        episode = db.get(EpisodeObject, their_result["episode_id"])
        their_episode = (episode.title, episode.summary, episode.tags_json)

    PATHS[path](http, sessions, made["memory_id"], "forget_request_00000005")

    assert_forgotten(sessions, first)
    with sessions() as db:
        episode = db.get(EpisodeObject, their_result["episode_id"])
        assert (episode.title, episode.summary, episode.tags_json) == their_episode


class _Stop(Exception):
    pass


class _AsPostgres:
    """The caller's session, reporting PostgreSQL and recording advisory locks instead of
    running them (SQLite has none)."""

    def __init__(self, db, log):
        self._db, self._log = db, log

    def get_bind(self):
        return type("Bind", (), {"dialect": type("Dialect", (), {"name": "postgresql"})})()

    def execute(self, statement, params=None, *args, **kwargs):
        if "pg_advisory_xact_lock" in str(statement):
            self._log.append(("lock", dict(params)))
            return None
        if params is None:
            return self._db.execute(statement, *args, **kwargs)
        return self._db.execute(statement, params, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._db, name)


def test_erase_takes_the_pipelines_text_lock_before_reading_the_backing_set(setup, monkeypatch):
    """On PostgreSQL a worker reusing the memory by its text links a fresh evidence row under
    pg_advisory_xact_lock(agent, lower(text)). The erase takes the same lock before it decides
    which evidence backed the memory, so that link is either seen (and its evidence forgotten
    too) or made after the memory is gone (and then it promotes a memory of its own)."""
    http, sessions, _, _ = setup
    _, made = listened(http, sessions, SECRET)
    log = []

    def sources(db, agent_id, memory_id):
        log.append("sources")
        raise _Stop

    monkeypatch.setattr(memory_forgetting, "forget_sources", sources)
    pipeline_log = []
    with sessions() as db:
        memory = db.get(Memory, made["memory_id"])
        with pytest.raises(_Stop):
            memory_forgetting.erase(_AsPostgres(db, log), "owner", memory, "delete", {})
        db.rollback()
    # The worker's key for the same words (another casing), taken from the pipeline itself.
    response = http.post(
        "/runtime/listeners/imports",
        headers={"X-MemoryGate-Listener-Key": LISTENER_KEY},
        json={"title": "ChatGPT: again", "content": SECRET.upper(), "tags": ["imported"]},
    )
    assert response.status_code == 200, response.text
    evidence_id = response.json()["evidence_id"]
    with sessions() as db:
        evidence = db.get(EvidenceObject, evidence_id)
        job = db.scalars(
            select(ProcessingJob).where(ProcessingJob.evidence_id == evidence_id)
        ).one()
        result = runtime_pipeline.process_evidence(
            _AsPostgres(db, pipeline_log), evidence, SECRET.upper(), job
        )
    assert result["memory_id"] == made["memory_id"]  # reused by its text
    lock = ("lock", {"agent_id": "owner", "memory_text": SECRET.strip().lower()})
    assert log == [lock, "sources"]
    assert lock in pipeline_log
