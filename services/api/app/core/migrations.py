"""Additive, idempotent schema patches for tables that existed before agent
isolation / observation lifecycle / the 4-type memory taxonomy / the
clarification-into-observation merge / entity dedup were added.

`Base.metadata.create_all` (called before this, in main.py) only creates
tables that don't exist yet — it never alters an existing table. A fresh
database gets the new columns for free straight from the model definitions.
An existing database needs these run once at startup to catch up.
"""
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.core.data_migrations import (
    _backfill_object_links,
    _merge_duplicate_entities,
    _migrate_clarifications,
    _migrate_memory_types,
    _table_exists,
)

_AGENT_ID_TABLES = ["memories", "entities", "observations", "patterns"]


def run_migrations(engine: Engine) -> None:
    with engine.begin() as conn:
        migrate_conversation_admission(conn)
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
        conn.execute(text("ALTER TABLE analysis_objects ADD COLUMN IF NOT EXISTS model TEXT, ADD COLUMN IF NOT EXISTS provider TEXT"))
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
            "result_json TEXT NOT NULL DEFAULT '{}', lease_owner TEXT NULL, "
            "lease_expires_at TIMESTAMPTZ NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        ))
        conn.execute(text("ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS lease_owner TEXT"))
        conn.execute(text("ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_processing_jobs_agent_id ON processing_jobs (agent_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_processing_jobs_evidence_id ON processing_jobs (evidence_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_processing_jobs_status ON processing_jobs (status)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_processing_jobs_lease_expires_at ON processing_jobs (lease_expires_at)"))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS vector_outbox ("
            "id TEXT PRIMARY KEY, agent_id TEXT NOT NULL DEFAULT 'default', "
            "object_kind TEXT NOT NULL, object_id TEXT NOT NULL, operation TEXT NOT NULL, "
            "document_text TEXT NULL, payload_json TEXT NOT NULL DEFAULT '{}', "
            "generation INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'pending', "
            "attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "last_error TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
            "CONSTRAINT uq_vector_outbox_object UNIQUE (object_kind, object_id))"
        ))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_vector_outbox_agent_id ON vector_outbox (agent_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_vector_outbox_object_kind ON vector_outbox (object_kind)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_vector_outbox_object_id ON vector_outbox (object_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_vector_outbox_status ON vector_outbox (status)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_vector_outbox_next_attempt_at ON vector_outbox (next_attempt_at)"))
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
        _backfill_vector_outbox(conn)


def migrate_conversation_admission(conn) -> None:
    """Add only nullable receipt metadata; never reinterpret old admission decisions."""
    schema = inspect(conn)
    if schema.has_table("conversation_receipts") and "admission_method" not in {
        column["name"] for column in schema.get_columns("conversation_receipts")
    }:
        conn.execute(text("ALTER TABLE conversation_receipts ADD COLUMN admission_method TEXT"))


def _backfill_vector_outbox(conn) -> None:
    """Give every pre-outbox object a one-time desired vector state."""
    sources = (
        (
            "memory",
            "memories",
            "text",
            (
                "json_build_object('agent_id', agent_id, 'memory_type', memory_type, "
                "'source_type', source_type, 'confidence', confidence, "
                "'tags', tags_json)"
            ),
        ),
        (
            "observation",
            "observations",
            "description",
            "json_build_object('agent_id', agent_id, 'signal_type', signal_type, 'status', status)",
        ),
        (
            "entity",
            "entities",
            "name",
            "json_build_object('agent_id', agent_id, 'entity_type', entity_type)",
        ),
    )
    for kind, table, document_column, payload_expression in sources:
        conn.execute(text(
            f"INSERT INTO vector_outbox (id, agent_id, object_kind, object_id, operation, "
            f"document_text, payload_json, generation, status, attempts, next_attempt_at, last_error) "
            f"SELECT md5('memorygate-vector:{kind}:' || id), agent_id, '{kind}', id, 'upsert', "
            f"{document_column}, ({payload_expression})::text, 1, 'pending', 0, now(), '' FROM {table} "
            "ON CONFLICT (object_kind, object_id) DO NOTHING"
        ))

    if _table_exists(conn, "deletion_receipts"):
        conn.execute(text(
            "INSERT INTO vector_outbox (id, agent_id, object_kind, object_id, operation, "
            "document_text, payload_json, generation, status, attempts, next_attempt_at, last_error) "
            "SELECT md5('memorygate-vector:' || object_kind || ':' || object_id), agent_id, "
            "object_kind, object_id, 'delete', NULL, '{}', 1, 'pending', 0, now(), '' "
            "FROM deletion_receipts WHERE object_kind IN ('memory', 'observation', 'entity') "
            "ON CONFLICT (object_kind, object_id) DO NOTHING"
        ))


def _rename_column(conn, table: str, old: str, new: str) -> None:
    old_exists = conn.execute(text(
        "SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": old}).first()
    new_exists = conn.execute(text(
        "SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": new}).first()

    if old_exists and not new_exists:
        conn.execute(text(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}"))
