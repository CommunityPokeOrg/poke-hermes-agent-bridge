# poke-hermes-agent-bridge

[![CI](https://github.com/CommunityPokeOrg/poke-hermes-agent-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/CommunityPokeOrg/poke-hermes-agent-bridge/actions/workflows/ci.yml)

A single-process bridge that exposes a [Hermes Agent](https://github.com/NousResearch/hermes-agent)
API server to [Poke](https://poke.com) — as an MCP integration for tool calls,
and as a REST API + signed webhooks for automations.

```
                ┌──────────────────────────┐
   Poke ──MCP──▶│                          │──▶ Hermes /v1/chat/completions
   Poke ──REST─▶│  poke-hermes-bridge      │──▶ Hermes /v1/runs (+SSE events)
   automations─▶│  :8700                   │        (stop / steer / approval)
  (HMAC hook)   │                          │
                │  callbacks ──────────────┼──▶ poke.com inbound api-message
                └──────────────────────────┘      (Bearer POKE_API_KEY)
```

## 5-minute quickstart (mock Hermes)

```bash
uv sync --extra dev
uv run poke-hermes-bridge gen-key            # -> KEY
cat > .env <<EOF
BRIDGE_API_KEYS=poke:$KEY
HERMES_API_KEY=mock-key-0123456789ab
HERMES_BASE_URL=http://127.0.0.1:8642
EOF
uv run poke-hermes-bridge mock-hermes &      # mock Hermes on :8642
uv run poke-hermes-bridge serve &            # bridge on :8700
curl -s localhost:8700/health/ready
curl -s -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "hello"}' localhost:8700/v1/tasks
```

## Real Hermes

Enable the API server in `~/.hermes/.env` (`API_SERVER_ENABLED=true`,
`API_SERVER_KEY=<>=16 chars>`), set `HERMES_API_KEY`/`HERMES_BASE_URL`, then
`poke-hermes-bridge check`. See [docs/hermes-setup.md](docs/hermes-setup.md).

## Poke integration

- MCP: `npx poke@latest tunnel http://localhost:8700/mcp -n "Hermes (dev)"`,
  or add at https://poke.com/integrations/new with URL `https://<bridge>/mcp`
  and a bridge API key. Tools: `hermes_ask` (sync), `hermes_start_task`,
  `hermes_task_status`, `hermes_stop_task`, `hermes_steer_task`, `hermes_health`.
- Callbacks: set `POKE_API_KEY` (V2 key) and async tasks can push results back
  via `{"type":"poke"}`. Webhooks get HMAC-signed POSTs.
- See [docs/poke-setup.md](docs/poke-setup.md) and [examples/poke/](examples/poke/).

## API summary

| Endpoint | Purpose |
|---|---|
| `GET /health`, `/health/ready` | liveness / Hermes reachability |
| `GET /v1/capabilities` | bridge + Hermes capability report |
| `POST /v1/tasks` | sync (200) or async (202) task |
| `GET /v1/tasks/{id}` | task record (per-key scoped) |
| `GET /v1/tasks/{id}/events` | SSE replay+live, `Last-Event-ID` resume |
| `POST /v1/tasks/{id}/stop|steer|approval` | control an active task |
| `POST /v1/webhooks/poke` | HMAC-signed inbound trigger (forced async) |
| `* /mcp` | streamable-HTTP MCP for Poke |

Errors: `{"error":{"code","message","request_id"}}`. Full reference:
[docs/protocol.md](docs/protocol.md).

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `BRIDGE_API_KEYS` | — (required) | comma-separated, `name:key` allowed, each >=16 chars |
| `BRIDGE_HOST` / `BRIDGE_PORT` | `0.0.0.0` / `8700` | |
| `BRIDGE_WEBHOOK_SECRET` | unset | enables `/v1/webhooks/poke` + webhook signing fallback |
| `BRIDGE_PUBLIC_URL` | unset | docs / `poke-link` only |
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

## Security

Bearer keys (constant-time), HMAC webhook auth with replay protection, SSRF
guarding on outbound callbacks, no secrets in logs or errors. **Hermes can run
arbitrary tools — treat bridge access as RCE-capable**; restrict Hermes
profiles/toolsets and isolate the network. Details: [docs/security.md](docs/security.md).

## Project layout

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

## License

MIT — see [LICENSE](LICENSE).
