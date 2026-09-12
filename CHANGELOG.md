# Changelog

## 0.1.0

Initial release.

- REST bridge protocol (`/v1/tasks`, SSE event stream, stop/steer/approval,
  capabilities, health/ready) with bearer auth, per-key task scoping,
  idempotency keys, rate limiting and a concurrency cap.
- Async tasks on Hermes `/v1/runs` with SSE follow + status polling, and a
  chat-completions fallback when `run_submission` is unavailable.
- Terminal callbacks: Poke inbound API and HMAC-signed webhooks with SSRF
  guard and retries.
- Signed inbound webhook `POST /v1/webhooks/poke` (HMAC + timestamp + replay
  cache) for Poke automations.
- MCP server at `/mcp` (streamable HTTP, bearer-protected) exposing
  `hermes_ask`, `hermes_start_task`, `hermes_task_status`, `hermes_stop_task`,
  `hermes_steer_task`, `hermes_health`.
- CLI: `serve`, `check`, `gen-key`, `poke-link`, `mock-hermes`.
- Deterministic mock Hermes for dev/tests; Dockerfile, compose, CI.
