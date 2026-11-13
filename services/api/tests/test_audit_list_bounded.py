"""The audit list is bounded: it returns the newest rows, at most `limit`."""

from app.models.audit import MemoryAudit
from memory_stack import stack as stack


def test_the_audit_list_returns_at_most_the_limit_newest_first(stack):
    with stack.sessions() as db:
        db.add_all([MemoryAudit(id=f"a{n:03d}", action="read", payload_json="{}") for n in range(30)])
        db.commit()

    assert len(stack.get("/audit?limit=10").json()) == 10
    assert len(stack.get("/audit?limit=0").json()) == 1
    assert len(stack.get("/audit").json()) == 30
    metrics = stack.get("/audit/metrics").json()
    assert metrics["by_action"]["read"] == 30 and metrics["reads"] == 30
