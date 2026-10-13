import contextlib
import json
import threading
import uuid
from datetime import UTC, datetime, timedelta

from app.core.config import PROCESSING_LEASE_SECONDS, PROCESSING_POLL_SECONDS
from app.core.db import SessionLocal
from app.models.evidence_object import EvidenceObject
from app.models.processing_job import ProcessingJob
from app.services.conversation_memory import index_pending
from app.services.processing_lease import LeaseHeartbeat, fence_job_lease
from app.services.runtime_pipeline import process_evidence
from sqlalchemy import and_, or_, select, update

_stop = threading.Event()
_thread: threading.Thread | None = None
_worker_id = str(uuid.uuid4())


def claim_next_job(db, worker_id: str, *, now: datetime | None = None,
                   lease_seconds: int = PROCESSING_LEASE_SECONDS) -> ProcessingJob | None:
    """Atomically claim the oldest runnable job.

    The conditional update is the concurrency boundary: workers may select the
    same candidate, but only one can change its still-runnable row. A legacy
    `processing` row without lease metadata is treated as abandoned.
    """
    claimed_at = now or datetime.now(UTC)
    expires_at = claimed_at + timedelta(seconds=lease_seconds)
    runnable = or_(
        ProcessingJob.status == "pending",
        and_(
            ProcessingJob.status == "processing",
            or_(
                ProcessingJob.lease_expires_at.is_(None),
                ProcessingJob.lease_expires_at <= claimed_at,
            ),
        ),
    )

    while True:
        job_id = db.execute(
            select(ProcessingJob.id)
            .where(runnable)
            .order_by(ProcessingJob.created_at.asc(), ProcessingJob.id.asc())
            .limit(1)
        ).scalar_one_or_none()
        if job_id is None:
            db.rollback()
            return None

        result = db.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job_id, runnable)
            .values(
                status="processing",
                attempts=ProcessingJob.attempts + 1,
                lease_owner=worker_id,
                lease_expires_at=expires_at,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            db.commit()
            return db.get(ProcessingJob, job_id)
        db.rollback()


def _run() -> None:
    while not _stop.is_set():
        db = SessionLocal()
        try:
            job = claim_next_job(db, _worker_id)
            if job:
                evidence = db.get(EvidenceObject, job.evidence_id)
                if not evidence:
                    fence_job_lease(db, job.id, job.lease_owner)
                    job.status = "failed"
                    job.error = "evidence no longer exists"
                    job.lease_owner = None
                    job.lease_expires_at = None
                    db.commit()
                else:
                    content = json.loads(evidence.normalized_payload_json or "{}").get("content") or evidence.summary
                    with LeaseHeartbeat(
                        SessionLocal,
                        job.id,
                        job.lease_owner,
                        lease_seconds=PROCESSING_LEASE_SECONDS,
                    ) as lease:
                        process_evidence(db, evidence, content, job, lease=lease)
        except Exception:
            db.rollback()
        finally:
            db.close()
        # Pending rows survive worker/database failure and are retried next poll.
        with contextlib.suppress(Exception):
            index_pending()
        _stop.wait(PROCESSING_POLL_SECONDS)


def start_worker() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, name="memorygate-processing", daemon=True)
    _thread.start()


def stop_worker() -> None:
    _stop.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=max(PROCESSING_POLL_SECONDS * 2, 1.0))
