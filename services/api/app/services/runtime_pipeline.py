import json
from datetime import UTC, datetime, timedelta

from app.models.analysis_object import AnalysisObject
from app.models.episode_object import EpisodeObject
from app.models.evidence_object import EvidenceObject
from app.models.memory import Memory
from app.models.object_link import ObjectLink
from app.models.processing_job import ProcessingJob
from app.services.classifier import classify_memory
from app.services.memory_truth import add_revision, detect_conflicts
from app.services.ollama_service import analyze_evidence
from app.services.qdrant_store import index_after_commit, upsert_memory_embedding
from app.services.signal_filter import score_value
from sqlalchemy import select


def _link(db, source_type, source_id, target_type, target_id, relationship, confidence=1.0):
    existing = db.execute(select(ObjectLink).where(
        ObjectLink.source_type == source_type, ObjectLink.source_id == source_id,
        ObjectLink.target_type == target_type, ObjectLink.target_id == target_id,
        ObjectLink.relationship == relationship,
    )).scalar_one_or_none()
    if existing:
        return existing
    row = ObjectLink(source_type=source_type, source_id=source_id, target_type=target_type,
                     target_id=target_id, relationship=relationship, confidence=confidence,
                     metadata_json="{}", created_by="runtime")
    db.add(row)
    return row


def process_evidence(db, evidence: EvidenceObject, content: str, job: ProcessingJob | None = None) -> dict:
    if job is None:
        job = ProcessingJob(agent_id=evidence.agent_id, evidence_id=evidence.id, status="pending")
        db.add(job)
        db.flush()
    job.status = "processing"
    job.attempts += 1
    job.stage = "episode"
    db.commit()
    try:
        cutoff = (evidence.occurred_at or datetime.now(UTC)) - timedelta(minutes=30)
        episode = db.execute(select(EpisodeObject).where(
            EpisodeObject.agent_id == evidence.agent_id,
            EpisodeObject.status == "open",
            EpisodeObject.occurred_start >= cutoff,
        ).order_by(EpisodeObject.occurred_start.desc()).limit(1)).scalar_one_or_none()
        if not episode:
            episode = EpisodeObject(
                agent_id=evidence.agent_id, title=evidence.title or f"{evidence.source_type} activity",
                summary=evidence.summary or content[:280], episode_type=evidence.source_type,
                confidence=evidence.integrity_confidence, tags_json=evidence.tags_json,
                occurred_start=evidence.occurred_at, occurred_end=evidence.occurred_at,
            )
            db.add(episode)
            db.flush()
        else:
            episode.occurred_end = evidence.occurred_at
        _link(db, "evidence", evidence.id, "episode", episode.id, "grouped_into", evidence.integrity_confidence)

        job.stage = "analysis"
        value = score_value(content)
        classification = classify_memory(content, "automatic_listener")
        analysis = AnalysisObject(
            agent_id=evidence.agent_id, analysis_type="deterministic_signal_extraction",
            evidence_ids_json=json.dumps([evidence.id]), input_summary=content[:500],
            output_summary=f"Value {value:.2f}; classified as {classification['memory_type']} ({classification['confidence']}).",
            steps_json=json.dumps(["normalize", "value_score", "memory_classification"]),
            confidence=min(evidence.integrity_confidence, 0.85 if value >= 0.3 else 0.6),
        )
        db.add(analysis)
        db.flush()
        _link(db, "episode", episode.id, "analysis", analysis.id, "analyzed_into", analysis.confidence)

        job.stage = "local_ai"
        ai_result = analyze_evidence(content, evidence.source_type)
        ai_analysis = None
        if ai_result:
            ai_analysis = AnalysisObject(
                agent_id=evidence.agent_id, analysis_type="local_ai_proposal",
                evidence_ids_json=json.dumps([evidence.id]), input_summary=content[:500],
                output_summary=ai_result["summary"],
                steps_json=json.dumps({"observations": ai_result["observations"], "memory_candidates": ai_result["memory_candidates"]}),
                confidence=min(evidence.integrity_confidence, 0.7),
            )
            db.add(ai_analysis)
            db.flush()
            _link(db, "episode", episode.id, "analysis", ai_analysis.id, "proposed_by", ai_analysis.confidence)

        memory = None
        indexing = {"status": "ok"}
        if value >= 0.3:
            memory = db.execute(select(Memory).where(Memory.agent_id == evidence.agent_id, Memory.text.ilike(content.strip()))).scalar_one_or_none()
            if not memory:
                memory = Memory(
                    agent_id=evidence.agent_id, text=content.strip(), summary=classification["summary"],
                    memory_type=classification["memory_type"], source_type="automatic_listener",
                    confidence=classification["confidence"], tags_json=evidence.tags_json,
                    valid_from=datetime.now(UTC),
                )
                db.add(memory)
                db.flush()
                add_revision(db, memory, "automatic listener promotion", "runtime")
                detect_conflicts(db, memory)
                indexing = index_after_commit(upsert_memory_embedding, memory.id, memory.text, payload={"agent_id": evidence.agent_id, "memory_type": memory.memory_type})
            _link(db, "analysis", analysis.id, "memory", memory.id, "supports", analysis.confidence)

        evidence.processing_state = "processed"
        job.status = "completed"
        job.stage = "complete"
        result = {"episode_id": episode.id, "analysis_id": analysis.id, "ai_analysis_id": ai_analysis.id if ai_analysis else None, "memory_id": memory.id if memory else None, "value_score": value}
        if indexing["status"] != "ok":
            # A job that promoted a memory the vector index never received is
            # not a clean success - the owner can see which ones to re-index.
            result["indexing"] = indexing
        job.result_json = json.dumps(result)
        db.commit()
        return result
    except Exception as exc:
        db.rollback()
        evidence = db.get(EvidenceObject, evidence.id)
        if evidence:
            evidence.processing_state = "quarantined"
        failed = db.get(ProcessingJob, job.id)
        if not failed:
            failed = ProcessingJob(id=job.id, agent_id=evidence.agent_id if evidence else "default", evidence_id=evidence.id if evidence else "", attempts=1)
            db.add(failed)
        failed.status = "failed"
        failed.error = str(exc)[:2000]
        db.commit()
        raise
