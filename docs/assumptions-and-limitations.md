# Assumptions and limitations

- **Hermes API surface pinned as observed on 2026-09 `main`** of
  NousResearch/hermes-agent. Event names may evolve; the bridge forwards
  unknown Hermes run events generically as `hermes.<event>`.
- **In-memory task store.** Tasks, events, and idempotency keys live in
  process memory with TTL eviction. Restarts lose them; run a single process.
  `TaskStore` is an ABC — a persistent implementation can be dropped in later.
- **Single process, no horizontal scaling.** Rate limits, the concurrency cap,
  and the task store are all per-process.
- **No multi-tenancy isolation beyond per-key task scoping.** Callers share
  the same Hermes instance; `conversation_id` maps into a caller-namespaced
  Hermes session key.
- **Hermes approvals need a human.** A task can sit in
  `waiting_for_approval` until someone calls `/v1/tasks/{id}/approval`.
- **Poke inbound API is fire-and-forget.** The bridge retries 3 times then
  records `last_error` on the task's callback status.
- **Sync mode uses chat completions** (stateless). Multi-turn state uses
  `conversation_id` -> `X-Hermes-Session-Key`.
- **MCP tools see only MCP-created tasks** (owner `mcp`); REST tasks are
  scoped to the creating API key.
