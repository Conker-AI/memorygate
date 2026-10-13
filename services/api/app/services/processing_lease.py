import threading
from datetime import UTC, datetime, timedelta

from app.core.config import PROCESSING_LEASE_SECONDS
from app.models.processing_job import ProcessingJob
from sqlalchemy import update


class LeaseLostError(RuntimeError):
    """Raised when a worker no longer owns a processing job."""


def renew_job_lease(
    db,
    job_id: str,
    lease_owner: str,
    *,
    now: datetime | None = None,
    lease_seconds: float = PROCESSING_LEASE_SECONDS,
) -> bool:
    renewed_at = now or datetime.now(UTC)
    result = db.execute(
        update(ProcessingJob)
        .where(
            ProcessingJob.id == job_id,
            ProcessingJob.status == "processing",
            ProcessingJob.lease_owner == lease_owner,
            ProcessingJob.lease_expires_at > renewed_at,
        )
        .values(lease_expires_at=renewed_at + timedelta(seconds=lease_seconds))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        return False
    db.commit()
    return True


def fence_job_lease(
    db,
    job_id: str,
    lease_owner: str,
    *,
    now: datetime | None = None,
) -> None:
    """Lock an unexpired owned lease in the caller's current transaction."""
    checked_at = now or datetime.now(UTC)
    with db.no_autoflush:
        result = db.execute(
            update(ProcessingJob)
            .where(
                ProcessingJob.id == job_id,
                ProcessingJob.status == "processing",
                ProcessingJob.lease_owner == lease_owner,
                ProcessingJob.lease_expires_at > checked_at,
            )
            .values(lease_expires_at=ProcessingJob.lease_expires_at)
            .execution_options(synchronize_session=False)
        )
    if result.rowcount != 1:
        db.rollback()
        raise LeaseLostError(f"processing lease lost for job {job_id}")


class LeaseHeartbeat:
    """Renew one claim until processing finishes or ownership is lost."""

    def __init__(
        self,
        session_factory,
        job_id: str,
        lease_owner: str,
        *,
        lease_seconds: float = PROCESSING_LEASE_SECONDS,
        interval_seconds: float | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.job_id = job_id
        self.lease_owner = lease_owner
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds or max(lease_seconds / 3, 0.05)
        self.lost = threading.Event()
        self.renewed = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "LeaseHeartbeat":
        self._thread = threading.Thread(
            target=self._run,
            name=f"memorygate-lease-{self.job_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                with self._session_factory() as db:
                    owned = renew_job_lease(
                        db,
                        self.job_id,
                        self.lease_owner,
                        lease_seconds=self.lease_seconds,
                    )
            except Exception:
                owned = False
            if not owned:
                self.lost.set()
                return
            self.renewed.set()

    def fence(self, db) -> None:
        if self.lost.is_set():
            db.rollback()
            raise LeaseLostError(f"processing lease lost for job {self.job_id}")
        fence_job_lease(db, self.job_id, self.lease_owner)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(self.interval_seconds * 2, 1.0))

    def __enter__(self) -> "LeaseHeartbeat":
        return self.start()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.stop()
