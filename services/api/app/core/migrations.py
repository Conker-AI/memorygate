"""Additive, idempotent schema patches for tables that existed before agent
isolation / observation lifecycle / the 4-type memory taxonomy / the
clarification-into-observation merge / entity dedup were added.

`Base.metadata.create_all` (called before this, in main.py) only creates
tables that don't exist yet — it never alters an existing table. A fresh
database gets the new columns for free straight from the model definitions.
An existing database needs these run once at startup to catch up.
"""
import json
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Engine

_AGENT_ID_TABLES = ["memories", "entities", "observations", "patterns"]

# old 7-type taxonomy -> current 4-type taxonomy (see services/classifier.py)
_LEGACY_MEMORY_TYPE_MAP = {
    "stable_preference": "fact",
    "identity_trait": "fact",
    "humor_style": "fact",
    "temporary_phase": "phase",
    "support_context": "phase",
    "task_context": "context",
    "harmful_pattern": "watch",
}
_CURRENT_MEMORY_TYPES = ("fact", "phase", "context", "watch", "skill")

# pending_clarifications.status -> the observation.status its merged row gets
_CLARIFICATION_STATUS_MAP = {
    "pending": "unconfirmed",
    "asked": "unconfirmed",
    "resolved": "confirmed",
    "dismissed": "archived",
}


def run_migrations(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE memories ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1"))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS auth_settings ("
            "id TEXT PRIMARY KEY, "
            "admin_key_hash TEXT NOT NULL DEFAULT ''"
            ")"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS ai_runtime_settings ("
            "id TEXT PRIMARY KEY, provider TEXT NOT NULL DEFAULT 'ollama', "
            "model TEXT NOT NULL DEFAULT 'qwen3:4b', api_key_encrypted TEXT NOT NULL DEFAULT '', "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            ")"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS evidence_sources ("
            "id TEXT PRIMARY KEY, "
            "source_key TEXT NOT NULL UNIQUE, "
            "source_type TEXT NOT NULL, "
            "label TEXT NOT NULL, "
            "description TEXT NOT NULL DEFAULT '', "
            "config_json TEXT NOT NULL DEFAULT '{}', "
            "secret_json TEXT NOT NULL DEFAULT '{}', "
            "enabled BOOLEAN NOT NULL DEFAULT true, "
            "last_ingested_at TIMESTAMPTZ NULL, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            ")"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_evidence_sources_source_key ON evidence_sources (source_key)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_evidence_sources_source_type ON evidence_sources (source_type)"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS evidence_objects ("
            "id TEXT PRIMARY KEY, "
            "source_id TEXT NOT NULL, "
            "source_key TEXT NOT NULL, "
            "source_type TEXT NOT NULL, "
            "title TEXT NOT NULL DEFAULT '', "
            "summary TEXT NOT NULL DEFAULT '', "
            "raw_payload_json TEXT NOT NULL DEFAULT '{}', "
            "normalized_payload_json TEXT NOT NULL DEFAULT '{}', "
            "tags_json TEXT NOT NULL DEFAULT '[]', "
            "integrity_confidence FLOAT NOT NULL DEFAULT 1.0, "
            "occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            ")"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_evidence_objects_source_key ON evidence_objects (source_key)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_evidence_objects_source_type ON evidence_objects (source_type)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_evidence_objects_occurred_at ON evidence_objects (occurred_at)"
        ))
        conn.execute(text("ALTER TABLE evidence_objects ADD COLUMN IF NOT EXISTS agent_id TEXT NOT NULL DEFAULT 'default'"))
        conn.execute(text("ALTER TABLE evidence_objects ADD COLUMN IF NOT EXISTS processing_state TEXT NOT NULL DEFAULT 'pending'"))
        conn.execute(text("ALTER TABLE evidence_objects ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ"))
        conn.execute(text("ALTER TABLE evidence_objects ADD COLUMN IF NOT EXISTS invalidation_reason TEXT NOT NULL DEFAULT ''"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_evidence_objects_agent_id ON evidence_objects (agent_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_evidence_objects_processing_state ON evidence_objects (processing_state)"))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS analysis_objects ("
            "id TEXT PRIMARY KEY, "
            "analysis_type TEXT NOT NULL, "
            "evidence_ids_json TEXT NOT NULL DEFAULT '[]', "
            "input_summary TEXT NOT NULL DEFAULT '', "
            "output_summary TEXT NOT NULL DEFAULT '', "
            "steps_json TEXT NOT NULL DEFAULT '[]', "
            "confidence FLOAT NOT NULL DEFAULT 0.5, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            ")"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_analysis_objects_analysis_type ON analysis_objects (analysis_type)"
        ))
        conn.execute(text("ALTER TABLE analysis_objects ADD COLUMN IF NOT EXISTS agent_id TEXT NOT NULL DEFAULT 'default'"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_analysis_objects_agent_id ON analysis_objects (agent_id)"))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS episode_objects ("
            "id TEXT PRIMARY KEY, "
            "agent_id TEXT NOT NULL DEFAULT 'default', "
            "title TEXT NOT NULL DEFAULT '', "
            "summary TEXT NOT NULL DEFAULT '', "
            "episode_type TEXT NOT NULL DEFAULT 'event', "
            "status TEXT NOT NULL DEFAULT 'open', "
            "confidence FLOAT NOT NULL DEFAULT 1.0, "
            "tags_json TEXT NOT NULL DEFAULT '[]', "
            "occurred_start TIMESTAMPTZ NULL, "
            "occurred_end TIMESTAMPTZ NULL, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            ")"
        ))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_episode_objects_agent_id ON episode_objects (agent_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_episode_objects_episode_type ON episode_objects (episode_type)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_episode_objects_status ON episode_objects (status)"))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS object_links ("
            "id TEXT PRIMARY KEY, "
            "source_type TEXT NOT NULL, "
            "source_id TEXT NOT NULL, "
            "target_type TEXT NOT NULL, "
            "target_id TEXT NOT NULL, "
            "relationship TEXT NOT NULL, "
            "confidence FLOAT NOT NULL DEFAULT 1.0, "
            "metadata_json TEXT NOT NULL DEFAULT '{}', "
            "created_by TEXT NOT NULL DEFAULT 'system', "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            ")"
        ))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_object_links_source ON object_links (source_type, source_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_object_links_target ON object_links (target_type, target_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_object_links_relationship ON object_links (relationship)"))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_object_links_path "
            "ON object_links (source_type, source_id, target_type, target_id, relationship)"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS processing_jobs ("
            "id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, evidence_id TEXT NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, "
            "stage TEXT NOT NULL DEFAULT 'ingest', error TEXT NOT NULL DEFAULT '', "
            "result_json TEXT NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        ))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_processing_jobs_agent_id ON processing_jobs (agent_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_processing_jobs_evidence_id ON processing_jobs (evidence_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_processing_jobs_status ON processing_jobs (status)"))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS agent_access_keys ("
            "id TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE, agent_id TEXT NOT NULL, key_hash TEXT NOT NULL, "
            "revoked BOOLEAN NOT NULL DEFAULT false, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_used_at TIMESTAMPTZ NULL)"
        ))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_access_keys_agent_id ON agent_access_keys (agent_id)"))
        conn.execute(text("ALTER TABLE memories ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'"))
        conn.execute(text("ALTER TABLE memories ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ"))
        conn.execute(text("ALTER TABLE memories ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_memories_status ON memories (status)"))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS memory_revisions ("
            "id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, changed_by TEXT NOT NULL DEFAULT 'system', "
            "reason TEXT NOT NULL DEFAULT '', snapshot_json TEXT NOT NULL DEFAULT '{}', "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        ))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_memory_revisions_memory_id ON memory_revisions (memory_id)"))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS memory_conflicts ("
            "id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, memory_id TEXT NOT NULL, conflicting_memory_id TEXT NOT NULL, "
            "reason TEXT NOT NULL DEFAULT '', confidence FLOAT NOT NULL DEFAULT 0.5, status TEXT NOT NULL DEFAULT 'open', "
            "resolved_by TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT now(), resolved_at TIMESTAMPTZ NULL)"
        ))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_memory_conflicts_agent_id ON memory_conflicts (agent_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_memory_conflicts_status ON memory_conflicts (status)"))

        for table in _AGENT_ID_TABLES:
            conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS agent_id TEXT NOT NULL DEFAULT 'default'"
            ))
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS ix_{table}_agent_id ON {table} (agent_id)"
            ))

        _rename_column(conn, "entities", "conker_notes", "agent_notes")
        _rename_column(conn, "entities", "conker_summary", "agent_summary")

        conn.execute(text(
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS exposure_count INTEGER NOT NULL DEFAULT 0"
        ))
        conn.execute(text(
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS max_exposures INTEGER NOT NULL DEFAULT 5"
        ))
        conn.execute(text(
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS confirmation_count INTEGER NOT NULL DEFAULT 0"
        ))
        conn.execute(text(
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS trigger_context TEXT NOT NULL DEFAULT ''"
        ))
        conn.execute(text(
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ"
        ))
        conn.execute(text(
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS archive_reason TEXT"
        ))

        _migrate_memory_types(conn)
        _migrate_clarifications(conn)
        _merge_duplicate_entities(conn)
        _backfill_object_links(conn)


def _table_exists(conn, name: str) -> bool:
    return conn.execute(text(
        "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
    ), {"t": name}).first() is not None


def _migrate_memory_types(conn) -> None:
    conn.execute(text(
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS do_not_generalize BOOLEAN NOT NULL DEFAULT false"
    ))
    conn.execute(text(
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS review_by TIMESTAMPTZ"
    ))

    # 'do_not_generalize' used to be (mis)written as a memory_type value by
    # some callers rather than the flag it always conceptually was - turn it
    # into the flag, with a neutral fallback type since the original intent
    # (what type it should have been) isn't recoverable from that alone.
    conn.execute(text(
        "UPDATE memories SET do_not_generalize = true, memory_type = 'context' "
        "WHERE memory_type = 'do_not_generalize'"
    ))

    # 'low_confidence' memories were never durable memories - they were
    # unconfirmed signals that landed in the wrong table. Move them to
    # observations (their proper home) instead of remapping their type.
    # Only columns that exist at this point in the migration sequence are
    # populated here; raise_condition/needs_clarification (added below by
    # _migrate_clarifications, which always runs after this) get their
    # column defaults applied retroactively to these rows too.
    low_confidence_rows = conn.execute(text(
        "SELECT id, agent_id, text, created_at FROM memories WHERE memory_type = 'low_confidence'"
    )).fetchall()
    for row in low_confidence_rows:
        conn.execute(text(
            """
            INSERT INTO observations (
                id, agent_id, session_id, observed_at, signal_type, description,
                raw_context, hypothesis, hypothesis_confidence, status, confirmed_by,
                entity_ids_json, related_observation_ids_json, confirmation_count,
                exposure_count, max_exposures, trigger_context
            ) VALUES (
                :id, :agent_id, '', :observed_at, 'verbal', :description,
                'migrated from a low_confidence memory row', '', 0.3, 'unconfirmed', '',
                '[]', '[]', 0, 0, 5, ''
            )
            """
        ), {
            "id": str(uuid.uuid4()),
            "agent_id": row.agent_id,
            "observed_at": row.created_at,
            "description": row.text,
        })
    if low_confidence_rows:
        conn.execute(text("DELETE FROM memories WHERE memory_type = 'low_confidence'"))

    for old_type, new_type in _LEGACY_MEMORY_TYPE_MAP.items():
        conn.execute(text(
            "UPDATE memories SET memory_type = :new WHERE memory_type = :old"
        ), {"new": new_type, "old": old_type})

    # anything still not one of the current types (an unrecognized legacy
    # value) falls back to 'context' rather than being left in an invalid
    # state the dashboard's 4 badge colors don't know how to render.
    placeholders = ", ".join(f"'{t}'" for t in _CURRENT_MEMORY_TYPES)
    conn.execute(text(
        f"UPDATE memories SET memory_type = 'context' WHERE memory_type NOT IN ({placeholders})"
    ))

    # phase memories need a review_by; default anything migrated in without one
    conn.execute(text(
        "UPDATE memories SET review_by = created_at + interval '14 days' "
        "WHERE memory_type = 'phase' AND review_by IS NULL"
    ))

    conn.execute(text("ALTER TABLE memories DROP COLUMN IF EXISTS identity_weight"))


def _migrate_clarifications(conn) -> None:
    conn.execute(text(
        "ALTER TABLE observations ADD COLUMN IF NOT EXISTS raise_condition TEXT NOT NULL DEFAULT ''"
    ))
    conn.execute(text(
        "ALTER TABLE observations ADD COLUMN IF NOT EXISTS needs_clarification BOOLEAN NOT NULL DEFAULT false"
    ))

    if not _table_exists(conn, "pending_clarifications"):
        return

    # this table predates agent isolation too - make sure agent_id is
    # populated before reading it out below, same as every other legacy table.
    conn.execute(text(
        "ALTER TABLE pending_clarifications ADD COLUMN IF NOT EXISTS agent_id TEXT NOT NULL DEFAULT 'default'"
    ))
    conn.execute(text(
        "ALTER TABLE pending_clarifications ADD COLUMN IF NOT EXISTS importance FLOAT NOT NULL DEFAULT 0.5"
    ))

    rows = conn.execute(text(
        "SELECT id, agent_id, session_id, observed_at, what_happened, hypotheses_json, "
        "status, resolved_answer, ask_after, importance, entity_ids_json, related_observation_ids_json "
        "FROM pending_clarifications"
    )).fetchall()

    for row in rows:
        raw_context = json.dumps({
            "migrated_from": "pending_clarifications",
            "hypotheses": json.loads(row.hypotheses_json) if row.hypotheses_json else [],
            "resolved_answer": row.resolved_answer,
            "importance": row.importance,
        })
        conn.execute(text(
            """
            INSERT INTO observations (
                id, agent_id, session_id, observed_at, signal_type, description,
                raw_context, hypothesis, hypothesis_confidence, status, confirmed_by,
                entity_ids_json, related_observation_ids_json, confirmation_count,
                exposure_count, max_exposures, trigger_context, raise_condition,
                needs_clarification
            ) VALUES (
                :id, :agent_id, :session_id, :observed_at, 'verbal', :description,
                :raw_context, '', :importance, :status, '',
                :entity_ids_json, :related_observation_ids_json, 0,
                0, 5, '', :raise_condition, true
            )
            """
        ), {
            "id": row.id,
            "agent_id": row.agent_id,
            "session_id": row.session_id,
            "observed_at": row.observed_at,
            "description": row.what_happened,
            "raw_context": raw_context,
            # hypothesis_confidence doubles as the importance-ordering signal
            # post-merge (see services/briefing.py) - importance was already 0-1.
            "importance": min(1.0, max(0.0, row.importance if row.importance is not None else 0.5)),
            "status": _CLARIFICATION_STATUS_MAP.get(row.status, "unconfirmed"),
            "entity_ids_json": row.entity_ids_json or "[]",
            "related_observation_ids_json": row.related_observation_ids_json or "[]",
            "raise_condition": row.ask_after or "",
        })

    conn.execute(text("DROP TABLE pending_clarifications"))


def _merge_duplicate_entities(conn) -> None:
    """One-time cleanup for entities created before POST /entity/create did
    dedup (see services/entity_dedup.py). Groups by (agent_id, entity_type,
    normalized name), keeps the oldest row per group as canonical, repoints
    every reference to it, and drops the rest. Naturally idempotent: once
    there's at most one row per group, the GROUP BY ... HAVING count(*) > 1
    below returns nothing.

    Doesn't touch Qdrant - the duplicate ids simply stop being referenced;
    stale points there are harmless (never returned once the Postgres row
    they'd resolve to is gone) and get cleaned up next time the API restarts
    and this function no-ops.
    """
    groups = conn.execute(text(
        """
        SELECT array_agg(id ORDER BY created_at, id) AS ids
        FROM entities
        GROUP BY agent_id, entity_type, lower(trim(name))
        HAVING count(*) > 1
        """
    )).fetchall()

    if not groups:
        return

    replace_map: dict[str, str] = {}

    for group in groups:
        ids = list(group.ids)
        canonical_id, duplicate_ids = ids[0], ids[1:]

        rows = conn.execute(text(
            "SELECT id, tags_json, attributes_json, description, agent_notes, agent_summary "
            "FROM entities WHERE id = ANY(:ids)"
        ), {"ids": ids}).fetchall()
        by_id = {row.id: row for row in rows}
        canonical = by_id[canonical_id]

        merged_tags = list(json.loads(canonical.tags_json))
        merged_attrs = dict(json.loads(canonical.attributes_json))
        description = canonical.description
        agent_notes = canonical.agent_notes
        agent_summary = canonical.agent_summary

        for dup_id in duplicate_ids:
            dup = by_id[dup_id]
            for tag in json.loads(dup.tags_json):
                if tag not in merged_tags:
                    merged_tags.append(tag)
            for key, value in json.loads(dup.attributes_json).items():
                merged_attrs.setdefault(key, value)
            description = description or dup.description
            agent_notes = agent_notes or dup.agent_notes
            agent_summary = agent_summary or dup.agent_summary
            replace_map[dup_id] = canonical_id

        conn.execute(text(
            "UPDATE entities SET tags_json = :tags, attributes_json = :attrs, "
            "description = :description, agent_notes = :notes, agent_summary = :summary "
            "WHERE id = :id"
        ), {
            "tags": json.dumps(merged_tags),
            "attrs": json.dumps(merged_attrs),
            "description": description,
            "notes": agent_notes,
            "summary": agent_summary,
            "id": canonical_id,
        })

        for dup_id in duplicate_ids:
            conn.execute(text("UPDATE entity_edges SET from_entity_id = :c WHERE from_entity_id = :d"), {"c": canonical_id, "d": dup_id})
            conn.execute(text("UPDATE entity_edges SET to_entity_id = :c WHERE to_entity_id = :d"), {"c": canonical_id, "d": dup_id})
            conn.execute(text("UPDATE entity_events SET entity_id = :c WHERE entity_id = :d"), {"c": canonical_id, "d": dup_id})
            conn.execute(text("UPDATE entity_history SET entity_id = :c WHERE entity_id = :d"), {"c": canonical_id, "d": dup_id})

    # entity_ids_json / applies_to_entity_ids_json are JSON arrays stored as
    # TEXT (not native jsonb), so membership rewrites happen in Python.
    for table, column in (("observations", "entity_ids_json"), ("patterns", "applies_to_entity_ids_json")):
        rows = conn.execute(text(f"SELECT id, {column} FROM {table}")).fetchall()
        for row in rows:
            ids = json.loads(getattr(row, column))
            if not any(i in replace_map for i in ids):
                continue
            new_ids = []
            seen = set()
            for i in ids:
                mapped = replace_map.get(i, i)
                if mapped not in seen:
                    seen.add(mapped)
                    new_ids.append(mapped)
            conn.execute(text(f"UPDATE {table} SET {column} = :v WHERE id = :id"), {"v": json.dumps(new_ids), "id": row.id})

    conn.execute(text("DELETE FROM entities WHERE id = ANY(:ids)"), {"ids": list(replace_map.keys())})


def _backfill_object_links(conn) -> None:
    """Materialize relationships already embedded in legacy JSON arrays."""
    specs = (
        ("analysis_objects", "evidence_ids_json", "evidence", "analysis", "analyzed_into"),
        ("patterns", "observation_ids_json", "observation", "pattern", "supports"),
        ("observations", "entity_ids_json", "observation", "entity", "about"),
        ("patterns", "applies_to_entity_ids_json", "pattern", "entity", "applies_to"),
    )
    for table, json_column, member_type, row_type, relationship in specs:
        if not _table_exists(conn, table):
            continue
        rows = conn.execute(text(f"SELECT id, {json_column} AS ids FROM {table}")).fetchall()
        for row in rows:
            try:
                member_ids = json.loads(row.ids or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            for member_id in member_ids:
                if relationship in ("about", "applies_to"):
                    source_type, source_id = row_type, row.id
                    target_type, target_id = member_type, member_id
                else:
                    source_type, source_id = member_type, member_id
                    target_type, target_id = row_type, row.id
                conn.execute(text(
                    "INSERT INTO object_links (id, source_type, source_id, target_type, target_id, relationship, confidence, metadata_json, created_by) "
                    "VALUES (:id, :st, :sid, :tt, :tid, :rel, 1.0, '{}', 'migration') "
                    "ON CONFLICT (source_type, source_id, target_type, target_id, relationship) DO NOTHING"
                ), {
                    "id": str(uuid.uuid4()), "st": source_type, "sid": source_id,
                    "tt": target_type, "tid": target_id, "rel": relationship,
                })


def _rename_column(conn, table: str, old: str, new: str) -> None:
    old_exists = conn.execute(text(
        "SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": old}).first()
    new_exists = conn.execute(text(
        "SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": new}).first()

    if old_exists and not new_exists:
        conn.execute(text(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}"))
