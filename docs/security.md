# MemoryGate security model

MemoryGate treats its dashboard as an administrative surface and keeps agents on a separate, read-only interface.

## Security Model

MemoryGate assumes the dashboard is an administrative surface and keeps agents on a separate read-only interface.

- **MemoryGate refuses to start with no admin key configured.** There is no open fallback tier; the
  startup error names the exact fix. A key supplied through `MEMORYGATE_ADMIN_KEY` must be at least
  16 characters.
- **CORS defaults to the bundled dashboard's own origins** (`http://localhost:8021`,
  `http://127.0.0.1:8021`). `MEMORYGATE_CORS_ORIGINS=*` is a development override only - a wildcard
  puts every route in reach of any page the owner has open, and it is logged as a warning at startup.
- Destructive actions need a second, deliberate confirmation on top of admin auth: `POST
  /system/memory-reset` requires the exact phrase `RESET MEMORY`. A valid admin key alone is not
  enough.
- Admin keys are stored as PBKDF2-SHA256 hashes, never plaintext.
- Failed key verification is limited to five attempts with a five-minute lockout per client scope.
- Agent read keys are separate, scoped credentials. They can retrieve context but cannot ingest, edit, reset, or administer MemoryGate.
- Listener ingestion uses a source-specific secret, not the admin key.
- LLMs receive no write, delete, shell, or tool capability through MemoryGate.
- OpenAI API keys, when configured, are encrypted at rest in the MemoryGate server volume and never returned to the dashboard after saving.
- Backups exclude admin/read-key hashes and listener secrets.

Local deployment protects against remote misuse, not a fully compromised host. Running the agent and MemoryGate services on separate machines is the recommended next isolation step.

## Bootstrap keys are setup, not rotation

Bootstrap read-key configuration is initial setup, not key rotation. Once its
label or credential exists, startup preserves the owner's revocation, agent
assignment and stored hash. Use the owner key-management API to issue replacement
credentials; changing bootstrap environment variables never restores authority.
Retain revoked key rows: they record the decision that restart must respect.
