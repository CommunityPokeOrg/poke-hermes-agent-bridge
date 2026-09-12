# poke-hermes-agent-bridge

[![CI](https://github.com/CommunityPokeOrg/poke-hermes-agent-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/CommunityPokeOrg/poke-hermes-agent-bridge/actions/workflows/ci.yml)

A single-process bridge that exposes a [Hermes Agent](https://github.com/NousResearch/hermes-agent)
API server to [Poke](https://poke.com) — as an MCP integration Poke can call
natively, plus a small REST API and signed webhooks for automations.

## Why this exists

Poke can drive external tools through **MCP integrations** — but Hermes Agent
isn't an MCP server. Hermes runs a gateway with an optional OpenAI-compatible
**API server** (`/v1/chat/completions`, `/v1/responses`) plus a **runs API**
(`/v1/runs`) with SSE event streaming, steering, and human approvals.

This bridge adapts that surface into a stable, secured protocol Poke can use:

- An **MCP endpoint** (`/mcp`, streamable HTTP) exposing `hermes_ask`,
  `hermes_start_task`, `hermes_task_status`, `hermes_stop_task`,
  `hermes_steer_task`, `hermes_health`.
- A **REST protocol** (`/v1/tasks`, SSE task events, stop/steer/approval) for
  anything else that wants to drive Hermes.
- **Result push-backs** to Poke via the inbound api-message API, so long-running
  agent work can report back asynchronously.
- A **signed inbound webhook** so Poke automations (or any HMAC-capable system)
  can trigger tasks.

## How it works

```
                ┌──────────────────────────┐
   Poke ──MCP──▶│                          │──▶ Hermes /v1/chat/completions
   Poke ──REST─▶│  poke-hermes-bridge      │──▶ Hermes /v1/runs (+SSE events)
   automations─▶│  :8700                   │        (stop / steer / approval)
  (HMAC hook)   │                          │
                │  callbacks ──────────────┼──▶ poke.com inbound api-message
                └──────────────────────────┘      (Bearer POKE_API_KEY)
```

### Sync flow (`hermes_ask`, `mode=sync`)

```
Poke            bridge                       Hermes
 │  hermes_ask    │                            │
 │──────────────▶│ POST /v1/chat/completions  │
 │               │───────────────────────────▶│
 │               │◀─────────────── reply ─────│
 │◀── text ──────│                            │
```

### Async flow (`hermes_start_task`, `mode=async`)

```
Poke            bridge                          Hermes            poke.com
 │ start task     │                               │                  │
 │───────────────▶│ POST /v1/runs                 │                  │
 │  202 + task_id │──────────────────────────────▶│                  │
 │◀───────────────│ GET /v1/runs/{id}/events (SSE)│                  │
 │                │◀──────────────────────────────│  tool/progress   │
 │                │ GET /v1/runs/{id} (poll)      │  events          │
 │                │──────────────────────────────▶│                  │
 │                │            run completed ◀────│                  │
 │                │ POST /api/v1/inbound/api-message ──────────────▶ │
 │◀── "Hermes finished task task_…" ─────────────────────────────────│
```

If Hermes doesn't advertise `run_submission`, async tasks fall back to a
background chat-completions call — the REST/SSE surface is unchanged.

## 5-minute quickstart (mock Hermes, no real agent needed)

```bash
uv sync --extra dev
KEY=$(uv run poke-hermes-bridge gen-key)
cat > .env <<EOF
BRIDGE_API_KEYS=poke:$KEY
HERMES_API_KEY=mock-key-0123456789ab
HERMES_BASE_URL=http://127.0.0.1:8642
EOF
uv run poke-hermes-bridge mock-hermes --port 8642 --api-key mock-key-0123456789ab &
uv run poke-hermes-bridge serve &        # bridge on :8700
curl -s localhost:8700/health/ready
curl -s -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "hello"}' localhost:8700/v1/tasks
# -> {"id":"task_...","status":"completed","output":"Hermes(mock) received: hello",...}
```

## Real Hermes

Enable the API server in `~/.hermes/.env` (`API_SERVER_ENABLED=true`,
`API_SERVER_KEY=<>=16 chars>`), set `HERMES_API_KEY`/`HERMES_BASE_URL`, then
`poke-hermes-bridge check` to verify connectivity. Full steps:
[docs/hermes-setup.md](docs/hermes-setup.md).

## Poke integration

```bash
# dev tunnel
npx poke@latest tunnel http://localhost:8700/mcp -n "Hermes (dev)"

# or register directly
npx poke@latest mcp add https://<your-bridge>/mcp -n "Hermes" -k <bridge-key>
```

Set `POKE_API_KEY` (a V2 key from Poke Kitchen) to let async tasks push their
results back to Poke automatically. See [docs/poke-setup.md](docs/poke-setup.md).

### Example: what a Poke user types

> "Ask Hermes to audit my repo's CI config and message me when it's done."

Poke calls `hermes_start_task(prompt="audit my repo's CI config …")`. The
bridge submits a Hermes run, returns a task id, follows the run's SSE event
stream, and when the run completes POSTs the result to Poke's inbound API —
so the user gets a message like
`"Hermes finished task task_7f2c… (completed)\n\n<audit output>"` without
polling.

## API examples

### Sync task

```bash
curl -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "summarise this diff", "conversation_id": "conv-1"}' \
  http://127.0.0.1:8700/v1/tasks
```

```json
{
  "id": "task_a6d0ed95a715d4fe",
  "status": "completed",
  "mode": "sync",
  "conversation_id": "conv-1",
  "created_at": 1789215438.25,
  "updated_at": 1789215438.25,
  "hermes_run_id": null,
  "output": "Hermes(mock) received: summarise this diff",
  "error": null,
  "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
  "metadata": null,
  "callback": null
}
```

### Async task

```bash
curl -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "research X", "mode": "async", "callback": {"type": "poke"}}' \
  http://127.0.0.1:8700/v1/tasks
# -> 202 {"id": "task_...", "status": "queued", ...}
```

### Task events (SSE)

```bash
curl -N -H "Authorization: Bearer $KEY" http://127.0.0.1:8700/v1/tasks/task_…/events
```

```
event: task.created
id: 1
data: {"task_id": "task_…", "mode": "async"}

event: task.status
id: 2
data: {"status": "running"}

event: hermes.tool.started
id: 3
data: {"event": "tool.started", "run_id": "run_…", "tool": "shell", "preview": "ls"}

event: task.completed
id: 4
data: {"output": "…", "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
```

### Error envelope

```json
{"error": {"code": "hermes_timeout", "message": "hermes did not respond in time", "request_id": "9f…"}}
```

## API summary

| Endpoint | Purpose |
|---|---|
| `GET /health`, `/health/ready` | liveness / Hermes reachability |
| `GET /v1/capabilities` | bridge + Hermes capability report |
| `POST /v1/tasks` | sync (200) or async (202) task; `Idempotency-Key` supported |
| `GET /v1/tasks/{id}` | task record (per-key scoped) |
| `GET /v1/tasks/{id}/events` | SSE replay+live, `Last-Event-ID` resume |
| `POST /v1/tasks/{id}/stop` | stop an active task |
| `POST /v1/tasks/{id}/steer` | `{"input": "..."}` while running |
| `POST /v1/tasks/{id}/approval` | `{"choice": "once|always|deny"}` |
| `POST /v1/webhooks/poke` | HMAC-signed inbound trigger (forced async) |
| `* /mcp` | streamable-HTTP MCP for Poke |

Full reference: [docs/protocol.md](docs/protocol.md).

## Conversation memory

`conversation_id` on a task maps to the Hermes session-key header
`X-Hermes-Session-Key: poke:<caller>:<conversation_id>` — per-caller, stable
memory scopes. Reuse the same `conversation_id` to continue a conversation.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `BRIDGE_API_KEYS` | — (required) | comma-separated, `name:key` allowed, each >=16 chars |
| `BRIDGE_HOST` / `BRIDGE_PORT` | `0.0.0.0` / `8700` | |
| `BRIDGE_WEBHOOK_SECRET` | unset | enables `/v1/webhooks/poke` + webhook signing fallback |
| `BRIDGE_PUBLIC_URL` | unset | docs / `poke-link` helper only |
| `BRIDGE_SYNC_TIMEOUT_SECONDS` / `BRIDGE_MAX_TIMEOUT_SECONDS` | 120 / 600 | |
| `BRIDGE_MAX_PROMPT_CHARS` | 32000 | |
| `BRIDGE_MAX_CONCURRENT_TASKS` | 8 | 429 `too_many_active_tasks` |
| `BRIDGE_RATE_LIMIT_PER_MINUTE` | 60 | per API key, 0 disables |
| `BRIDGE_TASK_TTL_SECONDS` / `BRIDGE_MAX_EVENTS_PER_TASK` | 3600 / 500 | |
| `BRIDGE_POLL_INTERVAL_SECONDS` | 5 | run-status safety-net poll |
| `BRIDGE_ALLOW_INSECURE_CALLBACKS` | false | disables https/SSRF callback guard |
| `BRIDGE_LOG_LEVEL` / `BRIDGE_LOG_PROMPTS` / `BRIDGE_CORS_ORIGINS` | INFO / false / none | |
| `HERMES_BASE_URL` / `HERMES_API_KEY` | `http://127.0.0.1:8642` / — (required) | |
| `HERMES_TIMEOUT_SECONDS` / `HERMES_VERIFY_TLS` / `HERMES_MODEL` | 120 / true / `hermes-agent` | |
| `POKE_API_KEY` / `POKE_INBOUND_URL` / `POKE_MAX_MESSAGE_CHARS` | unset / api-message / 8000 | |

## Docker

```bash
docker build -t poke-hermes-bridge .
docker run --env-file .env -p 8700:8700 poke-hermes-bridge

# or compose; the dev profile adds a mock Hermes on :8642
HERMES_BASE_URL=http://mock-hermes:8642 docker compose --profile dev up
```

## Security

Three trust zones — **Poke ⇄ bridge** (bearer keys from `BRIDGE_API_KEYS`,
constant-time compare), **bridge ⇄ Hermes** (`HERMES_API_KEY`, never logged or
returned in errors), and **bridge → Poke** (`POKE_API_KEY` on the inbound
api-message call). Inbound webhooks are HMAC-SHA256 signed with a timestamp
skew window and replay cache; outbound webhook callbacks require HTTPS and are
SSRF-guarded against private/loopback resolution. Logs never contain
Authorization headers, prompts, or outputs at INFO.

**Hermes can run arbitrary tools — treat bridge access as RCE-capable.**
Restrict Hermes profiles/toolsets, isolate the network, terminate TLS in front,
and rotate keys. Details: [docs/security.md](docs/security.md).

## Assumptions & limitations

- The Hermes API surface is pinned as observed on `hermes-agent` **main as of
  Sept 2026**; unknown run events are forwarded generically as `hermes.<event>`.
- **In-memory task store, single process** — no persistence, no horizontal
  scaling (a `TaskStore` ABC leaves room for a durable backend).
- **Hermes approvals need a human** — a task waits in `waiting_for_approval`
  until someone calls `/v1/tasks/{id}/approval`.
- **No multi-tenant isolation** beyond per-API-key task scoping.
- **Poke inbound API is fire-and-forget** — callbacks retry 3× then record
  `last_error` on the task.

See [docs/assumptions-and-limitations.md](docs/assumptions-and-limitations.md).

## Docs

| Doc | Contents |
|---|---|
| [docs/protocol.md](docs/protocol.md) | full request/response/SSE/error reference + state machine |
| [docs/security.md](docs/security.md) | trust boundaries, keys, HMAC, SSRF, hardening |
| [docs/hermes-setup.md](docs/hermes-setup.md) | enable + verify the Hermes API server |
| [docs/poke-setup.md](docs/poke-setup.md) | tunnels, integrations, callbacks |
| [docs/assumptions-and-limitations.md](docs/assumptions-and-limitations.md) | what to expect |
| [docs/development.md](docs/development.md) | dev workflow |
| [examples/poke/](examples/poke/) | curl script, Python client, webhook signing |

## Development

```bash
make install   # uv sync --extra dev
make lint      # ruff check + format --check + mypy src (strict)
make test      # pytest -q (no network; in-process mock Hermes)
make serve     # run the bridge
make mock      # run mock Hermes on :8642
```

Project layout:

```
src/poke_hermes_bridge/
  app.py            FastAPI app: routes, middleware, MCP mount
  service.py        task lifecycle shared by REST + MCP
  hermes/client.py  Hermes API client (typed errors)
  store.py          TaskStore ABC + in-memory impl
  callbacks.py      poke + webhook delivery, HMAC, SSRF guard
  mcp_server.py     FastMCP tools for Poke
  devtools/mock_hermes.py   deterministic mock Hermes
  cli.py            serve / check / gen-key / poke-link / mock-hermes
docs/  examples/poke/  tests/
```

## Roadmap

- Persistent TaskStore (Redis/SQLite) for multi-process deployments
- Streaming sync responses (`chat_stream` passthrough)
- Structured task progress surfaced back into Poke messages

## Contributing

Issues and PRs welcome. Run `make lint && make test` before submitting.

## License

MIT — see [LICENSE](LICENSE).
