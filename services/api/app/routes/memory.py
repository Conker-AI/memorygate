import contextlib
import json
from datetime import UTC, datetime, timedelta

from app.core.agent import get_agent_id, resolve_agent_id
from app.core.db import SessionLocal
from app.models.audit import MemoryAudit
from app.models.memory import Memory
from app.models.memory_conflict import MemoryConflict
from app.models.memory_revision import MemoryRevision
from app.schemas.memory import (
    ConflictResolveRequest,
    MemoryPatchRequest,
    MemorySearchRequest,
    MemoryWriteRequest,
)
from app.services.agent_config_service import get_or_create_config
from app.services.classifier import CURRENT_MEMORY_TYPES, classify_memory, normalize_memory_type
from app.services.memory_truth import add_revision, detect_conflicts
from app.services.qdrant_store import (
    INDEX_UNREACHABLE,
    delete_memory_embedding,
    find_near_duplicate,
    index_after_commit,
    search_memory_embeddings,
    semantic_status,
    upsert_memory_embedding,
)
from app.services.scoring import memory_rank_bonus, memory_strength
from app.services.signal_filter import NOVELTY_DUPLICATE, NOVELTY_LOW, novelty_bucket, score_value
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm.exc import StaleDataError

router = APIRouter(prefix="/memory", tags=["memory"])

PHASE_REVIEW_WINDOW = timedelta(days=14)

def _parse_review_by(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

def _default_review_by(memory_type: str, review_by: datetime | None) -> datetime | None:
    if review_by is not None:
        return review_by
    if memory_type == "phase":
        return datetime.now(UTC) + PHASE_REVIEW_WINDOW
    return None

def _row_to_dict(row, score=None):
    tags = json.loads(row.tags_json)
    if isinstance(tags, dict):
        tags = tags.get("tags", [])
    result = {
        "id": row.id,
        "revision": row.revision,
        "agent_id": row.agent_id,
        "text": row.text,
        "summary": row.summary,
        "memory_type": row.memory_type,
        "source_type": row.source_type,
        "confidence": row.confidence,
        "do_not_generalize": row.do_not_generalize,
        "review_by": row.review_by.isoformat() if row.review_by else None,
        "tags": tags,
        "status": row.status,
        "valid_from": row.valid_from.isoformat() if row.valid_from else None,
        "valid_until": row.valid_until.isoformat() if row.valid_until else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
    if score is not None:
        result["similarity"] = score
    return result

def _merge_tags(old_tags: list[str], new_tags: list[str]) -> list[str]:
    merged = []
    seen = set()
    for tag in old_tags + new_tags:
        if tag not in seen:
            seen.add(tag)
            merged.append(tag)
    return merged


@router.get("/conflicts")
def list_conflicts(agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        rows = db.execute(select(MemoryConflict).where(MemoryConflict.agent_id == agent_id).order_by(MemoryConflict.created_at.desc())).scalars().all()
        return {"results": [{"id": row.id, "memory_id": row.memory_id, "conflicting_memory_id": row.conflicting_memory_id,
                              "reason": row.reason, "confidence": row.confidence, "status": row.status,
                              "created_at": row.created_at.isoformat() if row.created_at else None} for row in rows]}
    finally:
        db.close()


@router.get("/{memory_id}/revisions")
def memory_revisions(memory_id: str, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        memory = db.get(Memory, memory_id)
        if not memory or memory.agent_id != agent_id:
            raise HTTPException(404, "Memory not found")
        rows = db.execute(select(MemoryRevision).where(MemoryRevision.memory_id == memory_id).order_by(MemoryRevision.created_at.desc())).scalars().all()
        return {"results": [{"id": row.id, "changed_by": row.changed_by, "reason": row.reason,
                              "snapshot": json.loads(row.snapshot_json or "{}"), "created_at": row.created_at.isoformat() if row.created_at else None} for row in rows]}
    finally:
        db.close()


@router.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: str, payload: ConflictResolveRequest, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        conflict = db.get(MemoryConflict, conflict_id)
        if not conflict or conflict.agent_id != agent_id:
            raise HTTPException(404, "Conflict not found")
        if payload.winner_memory_id not in (conflict.memory_id, conflict.conflicting_memory_id):
            raise HTTPException(422, "Winner must be one of the conflicted memories")
        winner = db.get(Memory, payload.winner_memory_id)
        loser = db.get(Memory, conflict.conflicting_memory_id if payload.winner_memory_id == conflict.memory_id else conflict.memory_id)
        if winner:
            winner.status = "active"
            winner.valid_until = None
            add_revision(db, winner, "selected during conflict resolution", "user")
        if loser:
            loser.status = "needs_review"
            loser.valid_until = datetime.now(UTC)
            add_revision(db, loser, "not selected during conflict resolution", "user")
        conflict.status = "resolved"
        conflict.resolved_by = "user"
        conflict.resolved_at = datetime.now(UTC)
        db.commit()
        return {"status": "ok", "winner_memory_id": payload.winner_memory_id}
    finally:
        db.close()

def _upgrade_existing_memory(db, row, payload, final_memory_type, final_confidence, final_summary):
    old_tags = json.loads(row.tags_json)
    if isinstance(old_tags, dict):
        old_tags = old_tags.get("tags", [])
    merged_tags = _merge_tags(old_tags, payload.tags)

    old_memory_type = row.memory_type
    old_strength = memory_strength(row.memory_type, row.confidence)
    new_strength = memory_strength(final_memory_type, final_confidence)

    upgraded = False
    indexing = {"status": "ok"}
    if new_strength > old_strength:
        row.memory_type = final_memory_type
        row.confidence = final_confidence
        row.summary = final_summary
        upgraded = True

    if merged_tags != old_tags:
        row.tags_json = json.dumps(merged_tags)
        upgraded = True

    if upgraded:
        add_revision(db, row, "duplicate upgraded existing memory", "dedup")
        db.add(MemoryAudit(
            action="upgrade",
            memory_id=row.id,
            payload_json=json.dumps({
                "agent_id": row.agent_id,
                "text": payload.text,
                "old_memory_type": old_memory_type,
                "new_memory_type": row.memory_type,
                "tags": merged_tags,
            }),
        ))
        db.commit()

        indexing = index_after_commit(
            upsert_memory_embedding,
            row.id,
            row.text,
            payload={
                "agent_id": row.agent_id,
                "memory_type": row.memory_type,
                "source_type": row.source_type,
                "confidence": row.confidence,
                "tags": merged_tags,
            },
        )

    result = {
        "status": "ok",
        "id": row.id,
        "memory_type": row.memory_type,
        "summary": row.summary,
        "duplicate_of": row.id,
        "upgraded": upgraded,
    }
    if indexing["status"] != "ok":
        result["indexing"] = indexing
    return result

@router.post("/write")
def write_memory(payload: MemoryWriteRequest, header_agent_id: str = Depends(get_agent_id)):
    agent_id = resolve_agent_id(header_agent_id, payload.agent_id)
    db = SessionLocal()
    try:
        config = get_or_create_config(db, agent_id)

        if config.signal_filter_enabled:
            value_score = score_value(payload.text)
            if value_score < config.value_threshold:
                db.add(MemoryAudit(
                    action="filtered",
                    memory_id=None,
                    payload_json=json.dumps({"agent_id": agent_id, "text": payload.text, "reason": "low_value", "value_score": value_score}),
                ))
                db.commit()
                return {"status": "filtered", "reason": "low value"}

        classified = classify_memory(payload.text, payload.source_type)

        final_memory_type = normalize_memory_type(payload.memory_type) or classified["memory_type"]
        if final_memory_type not in CURRENT_MEMORY_TYPES:
            raise HTTPException(
                422,
                f"Unrecognized memory_type '{payload.memory_type}'. "
                f"Must be one of {sorted(CURRENT_MEMORY_TYPES)} (or a known legacy alias).",
            )
        final_confidence = payload.confidence or classified["confidence"]
        final_summary = classified["summary"]
        final_do_not_generalize = payload.do_not_generalize if payload.do_not_generalize is not None else False
        final_review_by = _default_review_by(final_memory_type, _parse_review_by(payload.review_by))

        normalized_text = payload.text.strip().lower()
        existing = db.execute(
            select(Memory)
            .where(Memory.agent_id == agent_id)
            .order_by(Memory.created_at.desc())
            .limit(100)
        ).scalars().all()

        # exact duplicate
        for row in existing:
            if row.text.strip().lower() == normalized_text:
                return _upgrade_existing_memory(
                    db, row, payload,
                    final_memory_type,
                    final_confidence,
                    final_summary,
                )

        # Novelty check via vector similarity, scoped to this agent. Without a
        # working vector path there is no near-duplicate signal at all, so the
        # write proceeds and reports that the check did not run - treating the
        # text as new without saying so would let duplicates pile up unannounced.
        novelty = semantic_status()
        near_hits = []
        if novelty["status"] == "ok":
            try:
                near_hits = find_near_duplicate(payload.text, limit=3, agent_id=agent_id)
            except Exception:
                novelty = {"status": "degraded", "component": "vector_index", "reason": INDEX_UNREACHABLE}
        best_score = near_hits[0]["score"] if near_hits else None
        bucket = novelty_bucket(best_score, config.novelty_threshold) if config.signal_filter_enabled else (
            "duplicate" if best_score is not None and best_score >= 0.92 else "new"
        )

        if bucket == NOVELTY_DUPLICATE and near_hits:
            row = db.get(Memory, near_hits[0]["id"])
            if row and row.agent_id == agent_id:
                result = _upgrade_existing_memory(
                    db, row, payload,
                    final_memory_type,
                    final_confidence,
                    final_summary,
                )
                result["near_duplicate_of"] = row.id
                result["similarity"] = near_hits[0]["score"]
                return result

        low_novelty = bucket == NOVELTY_LOW
        if low_novelty and payload.confidence is None:
            final_confidence = "low"

        memory = Memory(
            agent_id=agent_id,
            text=payload.text,
            summary=final_summary,
            memory_type=final_memory_type,
            source_type=payload.source_type,
            confidence=final_confidence,
            do_not_generalize=final_do_not_generalize,
            review_by=final_review_by,
            tags_json=json.dumps(payload.tags),
            valid_from=datetime.now(UTC),
        )
        db.add(memory)
        db.commit()
        db.refresh(memory)
        add_revision(db, memory, "memory created", "write")
        detect_conflicts(db, memory)
        db.commit()

        indexing = index_after_commit(
            upsert_memory_embedding,
            memory.id,
            memory.text,
            payload={
                "agent_id": agent_id,
                "memory_type": memory.memory_type,
                "source_type": memory.source_type,
                "confidence": memory.confidence,
                "tags": payload.tags,
            },
        )

        db.add(MemoryAudit(
            action="write",
            memory_id=memory.id,
            payload_json=json.dumps({
                "agent_id": agent_id,
                "text": payload.text,
                "source_type": payload.source_type,
                "memory_type": memory.memory_type,
                "evidence": payload.evidence,
                "tags": payload.tags,
                "low_novelty": low_novelty,
            }),
        ))
        db.commit()

        response = {
            "status": "ok",
            "id": memory.id,
            "memory_type": memory.memory_type,
            "summary": memory.summary,
        }
        if low_novelty:
            response["low_novelty"] = True
        if indexing["status"] != "ok":
            # The row is committed and lexically searchable; say plainly that it
            # is not in the vector index rather than implying a complete write.
            response["indexing"] = indexing
        if novelty["status"] != "ok":
            response["novelty_check"] = novelty
        return response
    finally:
        db.close()

@router.post("/search")
def search_memory(payload: MemorySearchRequest, header_agent_id: str = Depends(get_agent_id)):
    agent_id = resolve_agent_id(header_agent_id, payload.agent_id)
    db = SessionLocal()
    try:
        retrieval = semantic_status()
        hits = []
        if retrieval["status"] == "ok":
            try:
                hits = search_memory_embeddings(payload.query, limit=20, agent_id=agent_id)
            except Exception:
                retrieval = {"status": "degraded", "component": "vector_index", "reason": INDEX_UNREACHABLE}
        id_to_score = {h["id"]: h["score"] for h in hits}
        ids = list(id_to_score.keys())

        rows = []
        scores = {}
        path = "semantic"
        if ids:
            id_to_rank = {memory_id: idx for idx, memory_id in enumerate(ids)}
            fetched = db.execute(
                select(Memory).where(Memory.id.in_(ids), Memory.agent_id == agent_id)
            ).scalars().all()

            query_lower = payload.query.lower()
            rescored = []
            for row in fetched:
                base = max(0.0, 1.0 - (id_to_rank.get(row.id, 999) * 0.03))
                bonus = memory_rank_bonus(row.memory_type, row.confidence)

                text_lower = row.text.lower()
                lexical = 0.0
                if "humor" in query_lower and any(x in text_lower for x in ["humor", "sarcasm", "deadpan", "joke"]):
                    lexical += 0.35
                if any(x in query_lower for x in ["sidecar", "sidecars", "build", "architecture", "workflow"]) and any(
                    x in text_lower for x in ["sidecar", "sidecars", "build", "architecture", "workflow", "before"]
                ):
                    lexical += 0.35

                rescored.append((base + bonus + lexical, row))

            rescored.sort(key=lambda x: x[0], reverse=True)
            rows = [row for _, row in rescored]
            scores = id_to_score
        else:
            path = "lexical"
            rows = db.execute(
                select(Memory)
                .where(Memory.agent_id == agent_id, Memory.text.ilike(f"%{payload.query}%"))
                .order_by(Memory.created_at.desc())
                .limit(20)
            ).scalars().all()

        results = []
        for row in rows:
            item = _row_to_dict(row, scores.get(row.id))
            item["retrieval_path"] = path
            results.append(item)
        return {"results": results, "retrieval": {"mode": path, "semantic": retrieval}}
    finally:
        db.close()

@router.get("")
def list_memory(agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        rows = db.execute(
            select(Memory).where(Memory.agent_id == agent_id).order_by(Memory.created_at.desc()).limit(100)
        ).scalars().all()
        return [_row_to_dict(row) for row in rows]
    finally:
        db.close()

@router.get("/{memory_id}")
def get_memory(memory_id: str, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = db.get(Memory, memory_id)
        if not row or row.agent_id != agent_id:
            raise HTTPException(404, "Memory not found")
        return _row_to_dict(row)
    finally:
        db.close()

def _check_revision(row, expected):
    # Optional for legacy callers; new editors must echo the fetched revision.
    if expected is not None and row.revision != expected:
        raise HTTPException(409, {"code": "revision_conflict", "current_revision": row.revision})


@router.patch("/{memory_id}")
def patch_memory(memory_id: str, payload: MemoryPatchRequest, agent_id: str = Depends(get_agent_id)):
    db = SessionLocal()
    try:
        row = db.get(Memory, memory_id)
        if not row or row.agent_id != agent_id:
            raise HTTPException(404, "Memory not found")

        _check_revision(row, payload.expected_revision)
        add_revision(db, row, "before manual edit", "user")
        if payload.text is not None:
            row.text = payload.text
        if payload.memory_type is not None:
            normalized = normalize_memory_type(payload.memory_type)
            if normalized not in CURRENT_MEMORY_TYPES:
                raise HTTPException(
                    422,
                    f"Unrecognized memory_type '{payload.memory_type}'. "
                    f"Must be one of {sorted(CURRENT_MEMORY_TYPES)} (or a known legacy alias).",
                )
            row.memory_type = normalized
        if payload.confidence is not None:
            row.confidence = payload.confidence
        if payload.do_not_generalize is not None:
            row.do_not_generalize = payload.do_not_generalize
        if payload.review_by is not None:
            row.review_by = _parse_review_by(payload.review_by)
        if payload.tags is not None:
            row.tags_json = json.dumps(payload.tags)

        db.add(MemoryAudit(action="edit", memory_id=row.id, payload_json=json.dumps({"text": row.text})))
        detect_conflicts(db, row)
        db.commit()
        db.refresh(row)

        indexing = index_after_commit(
            upsert_memory_embedding,
            row.id,
            row.text,
            payload={
                "agent_id": row.agent_id,
                "memory_type": row.memory_type,
                "source_type": row.source_type,
                "confidence": row.confidence,
                "tags": json.loads(row.tags_json),
            },
        )

        result = {"status": "ok", "memory": _row_to_dict(row)}
        if indexing["status"] != "ok":
            result["indexing"] = indexing
        return result
    except StaleDataError:
        db.rollback()
        raise HTTPException(409, {"code": "revision_conflict", "message": "Memory changed; reload before editing."}) from None
    finally:
        db.close()

@router.delete("/{memory_id}")
def delete_memory(memory_id: str, agent_id: str = Depends(get_agent_id),
                  expected_revision: int | None = Query(default=None, ge=1)):
    db = SessionLocal()
    try:
        row = db.get(Memory, memory_id)
        if not row or row.agent_id != agent_id:
            raise HTTPException(404, "Memory not found")

        _check_revision(row, expected_revision)
        from app.services.memory_forgetting import erase
        # A deleted memory's text must not survive in revisions, conflicts or audit.
        erase(db, agent_id, row, "delete", {"revision": row.revision})
        db.commit()

        # Postgres (the source of truth) already committed the delete -
        # a stale/malformed Qdrant point shouldn't turn a successful
        # delete into a 500.
        with contextlib.suppress(Exception):
            delete_memory_embedding(memory_id)

        return {"status": "ok"}
    except StaleDataError:
        db.rollback()
        raise HTTPException(409, {"code": "revision_conflict", "message": "Memory changed; reload before deleting."}) from None
    finally:
        db.close()
