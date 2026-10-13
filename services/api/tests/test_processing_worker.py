import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from app.core.db import Base
from app.models.evidence_object import EvidenceObject
from app.models.processing_job import ProcessingJob
from app.services import runtime_pipeline
from app.services.processing_lease import LeaseHeartbeat, LeaseLostError
from app.services.processing_worker import claim_next_job
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


def _database(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'processing.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    return engine, sessions


def test_claim_is_exclusive_across_workers(tmp_path):
    engine, sessions = _database(tmp_path)
    try:
        with sessions() as db:
            db.add(ProcessingJob(agent_id="owner", evidence_id="evidence-1", stage="queued"))
            db.commit()

        now = datetime.now(UTC)

        def claim(worker_id):
            with sessions() as db:
                job = claim_next_job(db, worker_id, now=now, lease_seconds=300)
                return job.id if job else None

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(claim, ("worker-a", "worker-b")))

        assert sum(job_id is not None for job_id in claims) == 1
        with sessions() as db:
            job = db.scalar(select(ProcessingJob))
            assert job.status == "processing"
            assert job.attempts == 1
            assert job.lease_owner in {"worker-a", "worker-b"}
    finally:
        engine.dispose()


def test_expired_claim_is_recovered_after_restart(tmp_path):
    engine, sessions = _database(tmp_path)
    try:
        started_at = datetime.now(UTC)
        with sessions() as db:
            db.add(ProcessingJob(agent_id="owner", evidence_id="evidence-1", stage="queued"))
            db.commit()

        with sessions() as db:
            first = claim_next_job(db, "worker-before-crash", now=started_at, lease_seconds=60)
            assert first is not None

        # A restarted process gets a new engine, session factory, and worker ID.
        engine.dispose()
        engine = create_engine(
            f"sqlite:///{tmp_path / 'processing.db'}",
            connect_args={"check_same_thread": False},
        )
        sessions = sessionmaker(engine, expire_on_commit=False, autoflush=False)

        with sessions() as db:
            assert claim_next_job(
                db, "worker-after-restart", now=started_at + timedelta(seconds=59), lease_seconds=60
            ) is None

        with sessions() as db:
            recovered = claim_next_job(
                db, "worker-after-restart", now=started_at + timedelta(seconds=61), lease_seconds=60
            )
            assert recovered is not None
            assert recovered.id == first.id
            assert recovered.attempts == 2
            assert recovered.lease_owner == "worker-after-restart"
    finally:
        engine.dispose()


def test_pipeline_releases_lease_on_completion(tmp_path, monkeypatch):
    engine, sessions = _database(tmp_path)
    try:
        monkeypatch.setattr(runtime_pipeline, "score_value", lambda _content: 0.0)
        monkeypatch.setattr(runtime_pipeline, "analyze_evidence", lambda *_args: None)
        with sessions() as db:
            evidence = EvidenceObject(
                agent_id="owner", source_id="source-1", source_key="listener",
                source_type="listener", summary="A bounded event",
                normalized_payload_json='{"content":"A bounded event"}',
            )
            db.add(evidence)
            db.flush()
            job = ProcessingJob(agent_id="owner", evidence_id=evidence.id, stage="queued")
            db.add(job)
            db.commit()

        with sessions() as db:
            claimed = claim_next_job(db, "worker-a")
            evidence = db.get(EvidenceObject, claimed.evidence_id)
            runtime_pipeline.process_evidence(db, evidence, "A bounded event", claimed)
            completed = db.get(ProcessingJob, claimed.id)
            assert completed.status == "completed"
            assert completed.attempts == 1
            assert completed.lease_owner is None
            assert completed.lease_expires_at is None
    finally:
        engine.dispose()


def test_legacy_processing_job_without_lease_is_recovered(tmp_path):
    engine, sessions = _database(tmp_path)
    try:
        with sessions() as db:
            db.add(ProcessingJob(
                agent_id="owner", evidence_id="evidence-1", status="processing", attempts=1,
                stage="analysis", lease_owner=None, lease_expires_at=None,
            ))
            db.commit()

        with sessions() as db:
            recovered = claim_next_job(db, "new-worker")
            assert recovered is not None
            assert recovered.attempts == 2
            assert recovered.lease_owner == "new-worker"
    finally:
        engine.dispose()


def test_heartbeat_protects_long_running_pipeline_past_original_deadline(tmp_path, monkeypatch):
    engine, sessions = _database(tmp_path)
    heartbeat = None
    release_analysis = threading.Event()
    analysis_started = threading.Event()
    try:
        with sessions() as db:
            evidence = EvidenceObject(
                agent_id="owner", source_id="source-1", source_key="listener",
                source_type="listener", summary="A bounded event",
                normalized_payload_json='{"content":"A bounded event"}',
            )
            db.add(evidence)
            db.flush()
            db.add(ProcessingJob(agent_id="owner", evidence_id=evidence.id, stage="queued"))
            db.commit()
            evidence_id = evidence.id
        with sessions() as db:
            claimed = claim_next_job(db, "worker-a", lease_seconds=1)
            original_expiry = claimed.lease_expires_at

        monkeypatch.setattr(runtime_pipeline, "score_value", lambda _content: 0.0)

        def slow_analysis(*_args):
            analysis_started.set()
            assert release_analysis.wait(timeout=2)
            return None

        monkeypatch.setattr(runtime_pipeline, "analyze_evidence", slow_analysis)
        heartbeat = LeaseHeartbeat(
            sessions,
            claimed.id,
            "worker-a",
            lease_seconds=1,
            interval_seconds=0.01,
        ).start()
        with ThreadPoolExecutor(max_workers=1) as pool:
            def process_claim():
                with sessions() as db:
                    evidence = db.get(EvidenceObject, evidence_id)
                    active_job = db.get(ProcessingJob, claimed.id)
                    return runtime_pipeline.process_evidence(
                        db, evidence, "A bounded event", active_job, lease=heartbeat
                    )

            future = pool.submit(process_claim)
            assert analysis_started.wait(timeout=1)
            assert heartbeat.renewed.wait(timeout=1)

            with sessions() as db:
                renewed = db.get(ProcessingJob, claimed.id)
                assert renewed.lease_expires_at > original_expiry
            with sessions() as db:
                contender = claim_next_job(
                    db,
                    "worker-b",
                    now=original_expiry + timedelta(milliseconds=1),
                    lease_seconds=1,
                )
                assert contender is None
            release_analysis.set()
            future.result(timeout=2)

        with sessions() as db:
            completed = db.get(ProcessingJob, claimed.id)
            assert completed.status == "completed"
            assert completed.lease_owner is None
    finally:
        release_analysis.set()
        if heartbeat:
            heartbeat.stop()
        engine.dispose()


def test_stale_owner_cannot_commit_completion(tmp_path, monkeypatch):
    engine, sessions = _database(tmp_path)
    try:
        started_at = datetime.now(UTC)
        with sessions() as db:
            evidence = EvidenceObject(
                agent_id="owner", source_id="source-1", source_key="listener",
                source_type="listener", summary="A bounded event",
                normalized_payload_json='{"content":"A bounded event"}',
            )
            db.add(evidence)
            db.flush()
            db.add(ProcessingJob(agent_id="owner", evidence_id=evidence.id, stage="queued"))
            db.commit()
            evidence_id = evidence.id

        with sessions() as db:
            claimed = claim_next_job(db, "worker-a", now=started_at, lease_seconds=60)

        monkeypatch.setattr(runtime_pipeline, "score_value", lambda _content: 0.0)

        def lose_lease(*_args):
            with sessions() as contender_db:
                replacement = claim_next_job(
                    contender_db,
                    "worker-b",
                    now=started_at + timedelta(seconds=61),
                    lease_seconds=60,
                )
                assert replacement is not None
            return None

        monkeypatch.setattr(runtime_pipeline, "analyze_evidence", lose_lease)
        lease = LeaseHeartbeat(sessions, claimed.id, "worker-a", lease_seconds=60)
        with sessions() as db:
            evidence = db.get(EvidenceObject, evidence_id)
            stale_job = db.get(ProcessingJob, claimed.id)
            with pytest.raises(LeaseLostError):
                runtime_pipeline.process_evidence(
                    db, evidence, "A bounded event", stale_job, lease=lease
                )

        with sessions() as db:
            job = db.get(ProcessingJob, claimed.id)
            evidence = db.get(EvidenceObject, evidence_id)
            assert job.status == "processing"
            assert job.lease_owner == "worker-b"
            assert job.stage == "local_ai"
            assert evidence.processing_state != "processed"
    finally:
        engine.dispose()


def test_stale_owner_cannot_commit_failure(tmp_path, monkeypatch):
    engine, sessions = _database(tmp_path)
    try:
        started_at = datetime.now(UTC)
        with sessions() as db:
            evidence = EvidenceObject(
                agent_id="owner", source_id="source-1", source_key="listener",
                source_type="listener", summary="A bounded event",
                normalized_payload_json='{"content":"A bounded event"}',
            )
            db.add(evidence)
            db.flush()
            db.add(ProcessingJob(agent_id="owner", evidence_id=evidence.id, stage="queued"))
            db.commit()
            evidence_id = evidence.id

        with sessions() as db:
            claimed = claim_next_job(db, "worker-a", now=started_at, lease_seconds=60)

        monkeypatch.setattr(runtime_pipeline, "score_value", lambda _content: 0.0)

        def lose_lease_and_fail(*_args):
            with sessions() as contender_db:
                replacement = claim_next_job(
                    contender_db,
                    "worker-b",
                    now=started_at + timedelta(seconds=61),
                    lease_seconds=60,
                )
                assert replacement is not None
            raise RuntimeError("old worker failed late")

        monkeypatch.setattr(runtime_pipeline, "analyze_evidence", lose_lease_and_fail)
        lease = LeaseHeartbeat(sessions, claimed.id, "worker-a", lease_seconds=60)
        with sessions() as db:
            evidence = db.get(EvidenceObject, evidence_id)
            stale_job = db.get(ProcessingJob, claimed.id)
            with pytest.raises(LeaseLostError):
                runtime_pipeline.process_evidence(
                    db, evidence, "A bounded event", stale_job, lease=lease
                )

        with sessions() as db:
            job = db.get(ProcessingJob, claimed.id)
            evidence = db.get(EvidenceObject, evidence_id)
            assert job.status == "processing"
            assert job.lease_owner == "worker-b"
            assert job.error == ""
            assert evidence.processing_state != "quarantined"
    finally:
        engine.dispose()
