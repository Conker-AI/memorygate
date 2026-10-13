import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://memorygate:memorygate_dev_password@memorygate-db:5432/memorygate",
)

QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "memories")

# The vector width the Qdrant collection is built at. It must match what the
# Embeddings sidecar serves - a mismatch writes vectors into an index of the
# wrong shape, which surfaces later as bad search results rather than an
# error. Startup compares the two and says so when they disagree.
# Default is Qwen3-Embedding-0.6B, the sidecar default. See ADR-0004.
EMBED_DIMENSION = int(os.getenv("EMBED_DIMENSION", "1024"))

MEMORYGATE_ADMIN_KEY = os.getenv("MEMORYGATE_ADMIN_KEY", "")

# The bundled dashboard is served from a different port than the API, so it
# needs an origin allowance - but only its own. `*` is a documented development
# override, never the default: combined with an open instance it put every
# route, /system/memory-reset included, in reach of any page in the owner's
# browser.
CORS_ANY_ORIGIN = "*"
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "MEMORYGATE_CORS_ORIGINS", "http://localhost:8021,http://127.0.0.1:8021"
    ).split(",")
    if origin.strip()
]

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://memorygate-ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b")
OLLAMA_ENABLED = os.getenv("OLLAMA_ENABLED", "true").lower() in {"1", "true", "yes"}
PROCESSING_POLL_SECONDS = float(os.getenv("PROCESSING_POLL_SECONDS", "2"))
PROCESSING_LEASE_SECONDS = max(120, int(os.getenv("PROCESSING_LEASE_SECONDS", "300")))
BACKUP_DIR = os.getenv("BACKUP_DIR", "/data/backups")
RUNTIME_SECRET_PATH = os.getenv("RUNTIME_SECRET_PATH", "/data/runtime-fernet.key")
