# AI runtime

The bounded model MemoryGate may use to analyse evidence and answer read-only questions.

## AI Runtime

MemoryGate supports two bounded model providers from **Settings -> AI Runtime**:

- **Ollama** is the optional local provider. Start the `local-ai` profile first, then select any installed local model, such as `qwen3:4b`.
- **OpenAI API** accepts a model identifier and an OpenAI API key. The key is sent only from MemoryGate's API server to `api.openai.com`; it is never stored in browser storage or exposed to an agent.

The selected model can:

- Propose observations and memory candidates from evidence.
- Answer a read-only Memory Lab question from retrieved context.

The selected model cannot:

- Write, delete, reset, or call tools through MemoryGate.
- Receive a hidden Memory Lab conversation history.
- Replace semantic retrieval or directly access PostgreSQL/Qdrant.

OpenAI uses the server-side Responses API with a Bearer API key. Keep the key private and treat provider usage as paid external processing. See the [OpenAI API quickstart](https://platform.openai.com/docs/quickstart/make-your-first-api-request) and [model catalog](https://developers.openai.com/api/docs/models).
