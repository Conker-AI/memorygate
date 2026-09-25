"""Offline deletion replay into isolated databases. Never removes the startup hold."""

import hashlib
import json
from datetime import UTC, datetime

from app.models.conversation_receipt import ConversationReceipt
from app.models.deletion_receipt import DeletionReceipt, RecoveryHold, RecoveryVerification, record
from app.models.entity import Entity
from app.models.episode_object import EpisodeObject
from app.models.memory import Memory
from app.models.object_link import ObjectLink
from app.models.observation import Observation
from app.services import conversation_memory
from sqlalchemy import select

MODELS = {"memory": Memory, "entity": Entity, "observation": Observation,
          "episode": EpisodeObject, "link": ObjectLink}


def reconcile_indexes(target_sessions, client, collections):
    """Delete and read back points on an explicitly supplied isolated index client.

    Services must remain stopped. No environment-configured live client is used.
    Receipt records verification, never releases the broader recovery hold.
    """
    if (set(collections) != {"memory", "entity", "observation"}
            or any(not isinstance(name, str) or not name for name in collections.values())
            or len(set(collections.values())) != 3):
        raise ValueError("Supply three distinct recovery collection names.")
    with target_sessions() as db:
        if db.get(RecoveryHold, "deletion-replay") is None:
            raise ValueError("Index reconciliation requires a held recovery.")
        previous = db.get(RecoveryVerification, "vector-deletions")
        if previous is not None:
            db.delete(previous)
            db.commit()
        indexes = set()
        for row in db.scalars(select(DeletionReceipt)):
            if row.object_kind in collections:
                if db.get(MODELS[row.object_kind], row.object_id) is not None:
                    raise ValueError("Replay database deletions before reconciling the index.")
                indexes.add((row.object_kind, row.object_id))
        for row in db.scalars(select(ConversationReceipt).where(ConversationReceipt.state == "deleted")):
            if row.memory_id:
                if db.get(Memory, row.memory_id) is not None:
                    raise ValueError("Deleted source still has a restored memory.")
                indexes.add(("memory", row.memory_id))
    encoded = json.dumps({"collections": collections, "points": sorted(indexes)}, sort_keys=True)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    try:
        present = {item.name for item in client.get_collections().collections}
        for kind, collection in collections.items():
            identities = sorted(identity for category, identity in indexes if category == kind)
            if collection not in present:
                continue
            for offset in range(0, len(identities), 100):
                batch = identities[offset:offset + 100]
                client.delete(collection_name=collection, points_selector=batch, wait=True)
                if client.retrieve(collection_name=collection, ids=batch,
                                   with_payload=False, with_vectors=False):
                    raise ValueError("Deleted vector points remain present.")
    except Exception:
        raise ValueError("Recovery index verification failed; retain the hold and retry.") from None
    with target_sessions() as db:
        row = db.get(RecoveryVerification, "vector-deletions")
        if row is None:
            row = RecoveryVerification(id="vector-deletions")
            db.add(row)
        row.evidence_digest, row.verified_at = digest, datetime.now(UTC)
        db.commit()
    return {"indexCleanupVerified": True, "evidenceDigest": digest,
            "pointCount": len(indexes), "recoveryHeld": True, "promotesRecovery": False}


def assert_not_held(db):
    if db.scalar(select(RecoveryHold.id).limit(1)):
        raise RuntimeError("MemoryGate recovery is held pending coordinated reconciliation.")


def evidence(source):
    """Only identities; an empty result is not proof of historical completeness."""
    rows = {(r.agent_id, r.object_kind, r.object_id) for r in source.scalars(select(DeletionReceipt))}
    # Older versions already retained these tombstones before the generic ledger.
    rows.update((r.agent_id, "conversation", r.message_id) for r in source.scalars(
        select(ConversationReceipt).where(ConversationReceipt.state == "deleted")))
    return sorted(rows)


def replay(receipts, target_sessions):
    """Operator supplies authoritative identities and an offline target factory.

    Each transaction is restartable. All index identities remain in receipts so
    retries retain cleanup evidence even when the database rows are already gone.
    """
    if (not isinstance(receipts, list) or len(receipts) > 1_000_000 or any(
        not isinstance(item, (list, tuple)) or len(item) != 3
        or any(not isinstance(part, str) or len(part) > 256 for part in item)
        or item[1] not in (*MODELS, "conversation") or not item[2]
        or (item[1] != "link" and not item[0]) for item in receipts)):
        raise ValueError("Invalid deletion evidence.")
    with target_sessions() as db:
        if db.get(RecoveryHold, "deletion-replay") is None:
            db.add(RecoveryHold(id="deletion-replay"))
        db.commit()
    indexes = set()
    for agent_id, kind, identity in sorted(set(tuple(item) for item in receipts)):
        if kind == "conversation":
            result = conversation_memory.forget(agent_id, identity, sessions=target_sessions)
            if result["memory_id"]:
                indexes.add(("memory", result["memory_id"]))
            continue
        with target_sessions() as db:
            model = MODELS[kind]
            row = db.get(model, identity)
            if row is not None and kind != "link" and row.agent_id != agent_id:
                raise ValueError("Restored object namespace conflicts with deletion evidence.")
            record(db, agent_id, kind, identity)
            if row is not None:
                if kind == "episode":
                    links = list(db.scalars(select(ObjectLink).where(
                        ((ObjectLink.source_type == "episode") & (ObjectLink.source_id == identity)) |
                        ((ObjectLink.target_type == "episode") & (ObjectLink.target_id == identity)))))
                    for link in links:
                        record(db, "", "link", link.id)
                        db.delete(link)
                db.delete(row)
            db.commit()
        if kind in ("memory", "entity", "observation"):
            indexes.add((kind, identity))
    return {"recoveryHeld": True, "coverage": "supplied-source-only",
            "indexDeletes": [{"kind": kind, "id": identity} for kind, identity in sorted(indexes)],
            "indexCleanupVerified": False, "promotesRecovery": False}
