# Security notes

## Trust boundaries

```
Poke ──(Bearer BRIDGE_API_KEYS, HTTPS)──▶ bridge ──(Bearer HERMES_API_KEY)──▶ Hermes
                                              │
                                              └─(Bearer POKE_API_KEY)──▶ poke.com inbound
```

- **Poke ⇄ bridge**: bearer keys from `BRIDGE_API_KEYS` (constant-time
  compare). Serve behind TLS (e.g. a tunnel or reverse proxy); the bridge warns
  at startup when bound to a non-loopback address.
- **bridge → Hermes**: `HERMES_API_KEY` is sent only to
  `HERMES_BASE_URL` and is never included in errors, responses, or INFO logs.
- **bridge → Poke**: `POKE_API_KEY` authorizes the inbound api-message call.
- **inbound webhooks**: HMAC-SHA256 over `timestamp.body` with a 300s skew
  window plus a replay cache.

## Hermes runs arbitrary tools — treat the bridge as RCE-capable

Anyone holding a bridge key can make Hermes execute tool calls. Mitigations:

- Restrict the Hermes toolset/profile on the agent side.
- Run Hermes and the bridge on an isolated network segment.
- Rotate `BRIDGE_API_KEYS`, `HERMES_API_KEY`, `POKE_API_KEY`, and
  `BRIDGE_WEBHOOK_SECRET` regularly.
- Keep `BRIDGE_ALLOW_INSECURE_CALLBACKS=false` in production.

## SSRF

Webhook callback URLs are validated: https-only and the hostname must not
resolve to private/loopback/link-local/reserved addresses
(`BRIDGE_ALLOW_INSECURE_CALLBACKS=true` disables this for local dev).

## Logging

Request logs include method, path, status, latency, request id — never the
Authorization header, prompts, or outputs at INFO. Set
`BRIDGE_LOG_PROMPTS=true` only for debugging.

## Data

The task store is in-memory: prompts, outputs, and metadata live in process
memory until TTL eviction (`BRIDGE_TASK_TTL_SECONDS`) or restart.
