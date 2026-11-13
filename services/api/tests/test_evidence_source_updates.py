"""Editing an evidence source keeps its listener working, and a disabled source takes nothing."""

import json

from app.models.evidence_source import EvidenceSource
from memory_stack import stack as stack
from sqlalchemy import select

SOURCE = {"source_key": "notes", "source_type": "manual", "label": "Notes"}


def _post(stack, path, body):
    return stack.http.post(path, headers=stack.admin_headers(), json=body)


def _secrets(stack):
    with stack.sessions() as db:
        row = db.execute(select(EvidenceSource).where(EvidenceSource.source_key == "notes")).scalar_one()
        return json.loads(row.secret_json)


def test_new_secrets_keep_the_listeners_ingest_key(stack):
    _post(stack, "/evidence/sources", SOURCE)
    key = _post(stack, "/evidence/sources/notes/rotate-ingest-key", {}).json()["ingest_key"]

    _post(stack, "/evidence/sources", {**SOURCE, "secrets": {"api_key": "abc"}})

    assert _secrets(stack) == {"ingest_key": key, "api_key": "abc"}


def test_a_disabled_source_takes_no_evidence_and_last_ingested_is_arrival_time(stack):
    _post(stack, "/evidence/sources", SOURCE)
    old = _post(stack, "/evidence", {"source_key": "notes", "title": "Old note", "summary": "from 2001",
                                    "occurred_at": "2001-01-01T00:00:00Z"})
    assert old.status_code == 200, old.text
    with stack.sessions() as db:
        source = db.execute(select(EvidenceSource).where(EvidenceSource.source_key == "notes")).scalar_one()
        assert source.last_ingested_at.year >= 2026

    _post(stack, "/evidence/sources", {**SOURCE, "enabled": False})
    refused = _post(stack, "/evidence", {"source_key": "notes", "title": "Late", "summary": "after disable"})

    assert refused.status_code == 409
    assert [row["title"] for row in stack.get("/evidence").json()["results"]] == ["Old note"]
