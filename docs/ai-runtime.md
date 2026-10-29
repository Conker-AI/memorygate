# AI runtime

The bounded model MemoryGate may use to analyse evidence and answer read-only questions.

## What the model does

MemoryGate makes two kinds of model call, both asking for JSON:

- **Evidence analysis** (`analyze_evidence`, max 160 tokens): proposes observations and memory
  candidates from one piece of evidence while the background worker processes it.
- **Memory Lab answers** (`answer_with_context`, max 220 tokens): answers a read-only question
  (`POST /runtime/ask`) from retrieved memory only.

Memory types are sorted by keyword signals (`services/classifier.py`), not by a model, and
vectors come from the Embeddings sidecar; neither is a model call covered here.

The model cannot:

- Write, delete, reset, or call tools through MemoryGate.
- Receive a hidden Memory Lab conversation history.
- Replace semantic retrieval or directly access PostgreSQL/Qdrant.

## Chosen in Conker (Pi model roles)

In Conker, Pi chooses the model, so the owner picks it once for every module in
**Conker: Settings, Models, Memory sorting**. Set both:

```bash
MEMORYGATE_PI_URL=http://pi:8050              # Pi's base URL, http or https
MEMORYGATE_PI_SERVICE_KEY=...                 # MemoryGate's Pi service key; a secret
```

With both set, every model call goes to Pi `POST /inference` with role `memory-sorting`, the
same system prompt, prompt and token limit as before, and `json: true`; the key goes only in the
`X-Pi-Service-Key` header and is never logged or returned. Until the owner picks a model there,
Pi answers with the same local model MemoryGate used, so nothing changes on upgrade.

MemoryGate's own model setting then decides nothing. `GET /system/ai-runtime` answers
`managed_by: "pi"`, `editable: false` and `chosen_in: "Chosen in Conker: Settings, Models,
Memory sorting"`; `PUT /system/ai-runtime` is refused with HTTP 409 and that sentence; the
dashboard's AI Runtime section shows the sentence instead of a Configure button;
`GET /runtime/health` reports the same under `model_choice`. The stored setting and its
encrypted key are kept, unread, for running MemoryGate without Pi.

When Pi has no answer (503 `no_model` or `unavailable`, a timeout after 90 seconds, a refusal,
or no reply), MemoryGate does exactly what it does when its local model is unavailable:
evidence processing finishes with the keyword classification only and an empty model proposal,
and `POST /runtime/ask` answers 503. Nothing is retried elsewhere and nothing is made up. Only
Pi's refusal code is logged, never the prompt, Pi's message, or the key.

Set only one of the two, or a URL without `http://` or `https://`, and MemoryGate warns at
startup and uses the direct path below.

## Without Pi (direct path)

Without the Pi settings, MemoryGate uses its own setting from **Settings -> AI Runtime**:

- **Ollama** is the optional local provider (`OLLAMA_URL`, `OLLAMA_ENABLED`). Start the
  `local-ai` profile first, then select any installed local model, such as `qwen3:4b`
  (the default).
- **OpenAI API** accepts a model identifier and an API key, which is encrypted on the
  MemoryGate server and never returned to the browser or an agent. Hosted generation is
  refused until it has a durable shared-budget adapter; each refusal is recorded in the audit
  as `hosted_generation_refused` (see [operations](operations.md)).
