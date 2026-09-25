# Operating MemoryGate

Ingesting evidence, backups, resets and the operational limits worth knowing.

## Evidence Ingestion

Create a source in **Sources & Evidence**, then send an event to its dedicated listener endpoint:

```text
POST /runtime/listeners/{source_key}
X-MemoryGate-Listener-Key: <source-specific secret>
```

An event becomes an immutable evidence object. With automatic processing enabled, it receives a processing job that may produce analysis, observations, and durable memory candidates. All resulting objects retain lineage rather than silently replacing their source.

## Backups and Reset

Settings provides logical JSON backups and a **Danger Zone**.

- **Create backup** exports memory data, lineage, and processing state to the persistent backup volume.
- **Reset all memory** removes all stored memory, evidence, entities, transcripts, analysis, episodes, processing records, and matching vector points.
- **Reset data from a date** removes records created on or after the selected date.

Every reset requires the current admin key and the exact phrase `RESET MEMORY`. A backup is created before any destructive change. Admin access, agent read keys, listener configuration, backups, and AI configuration are preserved.

## Operational Notes

- Keep all secrets out of Git. Use the Settings UI or server environment configuration.
- Do not expose the dashboard/API directly to the public internet. Put them behind a private network, VPN, or authenticated reverse proxy when leaving localhost.
- Backups are logical exports, not an encrypted disaster-recovery system. Protect the Docker volume and copy important backups to secure storage.
- MemoryGate can preserve evidence and history, but no automated system can guarantee a fact is true. Confidence, provenance, and review remain part of the design.

Direct OpenAI generation is refused until it has a durable shared-budget adapter.
Refusals appear in the owner audit as `hosted_generation_refused`, without prompt text.
Optional `MEMORYGATE_HOSTED_COST_QUOTE` JSON records an owner-supplied estimate:
`model`, `valid_until` (Unix seconds), HTTPS `source`, `input_token_ceiling`,
`input_per_million_microusd`, `output_per_million_microusd`. Without a current quote,
cost is explicitly unknown. An estimate never enables spending; choose local Ollama.

Qdrant health is degraded when any existing collection cannot be inspected or has an unknown vector dimension, even if collection listing succeeded.

Conversation ingestion above 16,000 characters returns HTTP 413 with `detail.code=CONTENT_TOO_LARGE`, `retryable=false`, and `max_content_characters=16000`. Preserve the original transcript; retries of the same oversized payload cannot succeed.

`cryptography` is pinned to 50.0.0: 48.0.1 fixes the bundled OpenSSL advisory,
but [the PKCS#7 advisory](https://github.com/pyca/cryptography/security/advisories/GHSA-g6cj-pr64-35w5)
requires 50.0.0. MemoryGate uses Fernet, so this is maintenance of a security dependency,
not a claim of a demonstrated vault exploit. Tests include the longstanding
[Fernet verification vector](https://github.com/fernet/spec/blob/master/verify.json).
