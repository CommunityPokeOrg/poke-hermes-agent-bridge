# Bridge protocol reference

Protocol version: `poke-hermes-bridge/1`. Base URL default `http://127.0.0.1:8700`.

## Auth

All `/v1/*` endpoints except `/v1/webhooks/poke` require
`Authorization: Bearer <key>` where `<key>` is one of `BRIDGE_API_KEYS`.
Errors share one envelope:

```json
{"error": {"code": "snake_case", "message": "human readable", "request_id": "..."}}
```

Every response carries `X-Request-Id` (echoed from the request if provided,
else generated).

## Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/health` | no | `{"status":"ok","service":"poke-hermes-bridge","version":...}` |
| GET | `/health/ready` | no | probes Hermes; 200 ready / 503 degraded |
| GET | `/v1/capabilities` | yes | bridge + cached Hermes capabilities |
| POST | `/v1/tasks` | yes | create task (200 sync, 202 async) |
| GET | `/v1/tasks/{id}` | yes | task record (per-key scoping, 404 otherwise) |
| GET | `/v1/tasks/{id}/events` | yes | SSE stream (replay + live, `Last-Event-ID`) |
| POST | `/v1/tasks/{id}/stop` | yes | stop an active task |
| POST | `/v1/tasks/{id}/steer` | yes | steer a running task |
| POST | `/v1/tasks/{id}/approval` | yes | resolve a Hermes approval |
| POST | `/v1/webhooks/poke` | HMAC | inbound webhook, forces async mode |
| * | `/mcp` | yes | streamable-HTTP MCP endpoint for Poke |

## POST /v1/tasks

```json
{
  "prompt": "string, 1..BRIDGE_MAX_PROMPT_CHARS",
  "instructions": "system prompt | null",
  "conversation_id": "^[A-Za-z0-9._:-]{1,128}$ | null",
  "mode": "sync | async",
  "timeout_seconds": 120,
  "callback": {"type": "poke"} | {"type": "webhook", "url": "https://...", "secret": "..."},
  "metadata": {"k": "v, <=16 keys, <=512 chars each"}
}
```

- `conversation_id` maps to Hermes `X-Hermes-Session-Key` =
  `poke:<caller_name>:<conversation_id>`.
- `Idempotency-Key` header: identical body within the task TTL returns the
  original task (200); a different body returns 409 `idempotency_conflict`.
- `mode=sync` waits up to `timeout_seconds` (capped at
  `BRIDGE_MAX_TIMEOUT_SECONDS`), returning a completed `Task`. Timeout -> 504
  `hermes_timeout`; Hermes down/5xx -> 502 `hermes_unavailable`; Hermes 401 ->
  502 `hermes_auth_failed` (the Hermes key is never echoed).
- `mode=async` submits a Hermes run when `run_submission` is advertised, else
  falls back to a background chat completion. Returns 202 with a `queued` task.

### Task object

```json
{
  "id": "task_<hex>",
  "status": "queued|running|waiting_for_approval|stopping|completed|failed|cancelled",
  "mode": "sync|async",
  "conversation_id": "...|null",
  "created_at": 0.0, "updated_at": 0.0,
  "hermes_run_id": "run_<hex>|null",
  "output": "...|null", "error": "...|null",
  "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
  "metadata": {},
  "callback": {"type": "poke", "delivered": true, "attempts": 1, "last_error": null}
}
```

## GET /v1/tasks/{id}/events (SSE)

Frames: `id: <seq>\nevent: <name>\ndata: {json}\n\n`. Bridge events:
`task.created`, `task.status` `{status}`, `task.completed` `{output, usage}`,
`task.failed` `{error}`, `task.events_truncated` (event buffer overflow marker).
Forwarded Hermes events arrive as `hermes.<event>` with the original payload
(e.g. `hermes.tool.started`, `hermes.run.completed`). `: keepalive` comment
every 15s. `Last-Event-ID: <seq>` resumes after a sequence number. The stream
replays buffered events first, then live events until the task is terminal.

## Control endpoints

`POST /v1/tasks/{id}/stop` -> proxies Hermes stop; 409 `task_not_active` when
terminal. `POST /v1/tasks/{id}/steer` `{"input": "..."}` -> 409
`task_not_accepting_steer` unless running. `POST /v1/tasks/{id}/approval`
`{"choice": "once|always|deny", "request_id"?}`.

## POST /v1/webhooks/poke

No bearer. Requires `X-Bridge-Timestamp` (unix seconds, ±300s) and
`X-Bridge-Signature: sha256=<hex hmac-sha256(BRIDGE_WEBHOOK_SECRET, "<ts>.<raw body>")>`.
Replays (same timestamp+signature) -> 409 `replay_detected`. Body is a
`TaskCreate`; mode is forced `async`, callback defaults to `{"type":"poke"}`
when `POKE_API_KEY` is configured. 404 when `BRIDGE_WEBHOOK_SECRET` is unset.

## Callbacks

- `{"type":"poke"}`: POST `POKE_INBOUND_URL` with `Bearer POKE_API_KEY`, body
  `{"message", "source":"poke-hermes-bridge", "task_id", "status", "conversation_id", "metadata"}`.
  Output truncated to `POKE_MAX_MESSAGE_CHARS`.
- `{"type":"webhook","url","secret"?}`: POST the `Task` JSON with
  `X-Bridge-Timestamp`, `X-Bridge-Signature` (secret or `BRIDGE_WEBHOOK_SECRET`),
  `X-Bridge-Event: task.completed|task.failed|task.cancelled`. HTTPS only and
  no private/loopback resolution unless `BRIDGE_ALLOW_INSECURE_CALLBACKS=true`.

Retries: 3 attempts, backoff 1s/4s/16s; `delivered`/`attempts`/`last_error`
recorded on the task.

## Rate limits and capacity

Token bucket per API key (`BRIDGE_RATE_LIMIT_PER_MINUTE`) -> 429 `rate_limited`
with `Retry-After`. Active non-terminal tasks capped at
`BRIDGE_MAX_CONCURRENT_TASKS` -> 429 `too_many_active_tasks`.

## Task state machine

```
              submit(async)
                  |
                queued -----> running ----------> completed
                  |             |    \
                  |             |     +-> waiting_for_approval -+-> running
                  |             |     +-> stopping -----------+-> cancelled
                  |             |     +-> failed
                  +-- fallback chat (no run_submission): same terminal states
```

Terminal states: `completed`, `failed`, `cancelled`.
