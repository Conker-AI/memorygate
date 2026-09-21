import logging
import os
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import CORS_ANY_ORIGIN, CORS_ORIGINS
from app.core.db import Base, database_health, engine
from app.core.migrations import run_migrations
from app.core.auth import require_key
from app.routes.memory import router as memory_router
from app.routes.audit import router as audit_router
from app.routes.entity import router as entity_router
from app.routes.observation import router as observation_router
from app.routes.pattern import router as pattern_router
from app.routes.agent_config import router as agent_config_router
from app.routes.briefing import router as briefing_router
from app.routes.transcript import router as transcript_router
from app.routes.auth_settings import router as auth_settings_router
from app.routes.evidence import router as evidence_router
from app.routes.lineage import router as lineage_router
from app.routes.runtime import router as runtime_router
from app.routes.conversation import router as conversation_router
from app.routes.corrections import router as corrections_router
from app.routes.system import router as system_router
from app.routes.skills import context_router as skills_context_router
from app.routes.skills import router as skills_router
from app.models import memory, audit, agent_config
from app.models import auth_setting
from app.models import evidence_source, evidence_object, analysis_object
from app.models import episode_object, object_link
from app.models import processing_job
from app.models import entity
from app.models import observation
from app.models import pattern
from app.models import session_transcript
from app.models import ai_runtime_setting
from app.services.qdrant_store import ensure_qdrant_collection, ensure_observation_collection, ensure_entity_collection, qdrant_health
from app.services.embeddings import embedding_health
from app.services.processing_worker import start_worker, stop_worker
from app.services.auth_settings_service import assert_admin_key_configured, ensure_bootstrap_agent_access_key

log = logging.getLogger("memorygate")

SERVICE_VERSION = "0.3.0"

app = FastAPI(title="MemoryGate", version=SERVICE_VERSION)

# Only the bundled dashboard's own origin by default. `*` is available as an
# explicit development override via MEMORYGATE_CORS_ORIGINS and is logged when
# used, because a wildcard puts every route in reach of any page the owner has
# open. See README, "CORS".
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# `/health` is unauthenticated, so its probes are cached briefly rather than
# letting an anonymous caller drive one dependency round trip per request.
# not_configured is healthy: it means "not set up", not "broken".
HEALTHY_STATUSES = {"ok", "not_configured"}
HEALTH_CACHE_SECONDS = 5.0
_health_cache: dict = {}


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        # Secure by default, or refuse to start. Raising here stops uvicorn with
        # the exact fix in the log rather than serving every route openly.
        key_source = assert_admin_key_configured(db)
        log.info("Admin key source: %s", key_source)
        bootstrap_read_key = os.environ.get("MEMORYGATE_BOOTSTRAP_READ_KEY", "").strip()
        if bootstrap_read_key:
            bootstrap_agent_id = os.environ.get("MEMORYGATE_BOOTSTRAP_AGENT_ID", "agent_pi_operator").strip() or "agent_pi_operator"
            ensure_bootstrap_agent_access_key(db, bootstrap_read_key, bootstrap_agent_id)
    finally:
        db.close()
    # Qdrant and the embedding provider are reported, not required. MemoryGate
    # serves lexical retrieval without them and says so on every response;
    # crashing instead would take the whole memory boundary down with the index.
    for ensure in (ensure_qdrant_collection, ensure_observation_collection, ensure_entity_collection):
        try:
            ensure()
        except Exception:
            log.warning("Vector index unreachable at startup; collections will be created on first use.")
            break
    provider = embedding_health()
    if provider["status"] != "ok":
        log.warning("Semantic retrieval is degraded: %s. Falling back to lexical search.", provider["reason"])
    if CORS_ANY_ORIGIN in CORS_ORIGINS:
        log.warning(
            "CORS is set to allow any origin. This is a development override; "
            "set MEMORYGATE_CORS_ORIGINS to the dashboard origin for normal use."
        )
    start_worker()

@app.on_event("shutdown")
def shutdown():
    stop_worker()

@app.get("/health")
def health():
    """Real dependency probes. Reason stays coarse - this route has no auth.

    Shape is fixed by the Conker module contract - see docs/module-contract.md.
    The container is `checks`, keyed by name, in every module: one dashboard has
    to render any of them without a per-module special case.
    """
    now = time.monotonic()
    cached = _health_cache.get("result")
    if cached and now - _health_cache["checked_at"] < HEALTH_CACHE_SECONDS:
        return {**cached, "age_seconds": round(now - _health_cache["checked_at"], 1)}
    checks = {
        "postgres": database_health(),
        "qdrant": qdrant_health(),
        "embeddings": embedding_health(),
    }
    # not_configured is not a failure. "Nothing here yet" and "it broke" are
    # different facts, and collapsing them is how a dashboard starts lying.
    degraded = sorted(name for name, probe in checks.items() if probe["status"] not in HEALTHY_STATUSES)
    result = {
        "service": "memorygate",
        "version": SERVICE_VERSION,
        "status": "degraded" if degraded else "ok",
        "degraded": degraded,
        "checks": checks,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    _health_cache["result"] = result
    _health_cache["checked_at"] = now
    return {**result, "age_seconds": 0.0}

@app.get("/auth/check")
def auth_check(tier: str = Depends(require_key)):
    return {"tier": tier}

_auth = [Depends(require_key)]

app.include_router(auth_settings_router, dependencies=_auth)
app.include_router(evidence_router, dependencies=_auth)
app.include_router(lineage_router, dependencies=_auth)
app.include_router(runtime_router)
app.include_router(conversation_router)
app.include_router(corrections_router)
app.include_router(system_router, dependencies=_auth)
app.include_router(memory_router, dependencies=_auth)
app.include_router(skills_router, dependencies=_auth)
app.include_router(skills_context_router)
app.include_router(audit_router, dependencies=_auth)
app.include_router(entity_router, dependencies=_auth)

app.include_router(observation_router, dependencies=_auth)

app.include_router(pattern_router, dependencies=_auth)

app.include_router(agent_config_router, dependencies=_auth)

app.include_router(briefing_router, dependencies=_auth)

app.include_router(transcript_router, dependencies=_auth)
