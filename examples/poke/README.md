# Poke examples

## 1. Run the bridge with mock Hermes

```bash
cp .env.example .env   # set BRIDGE_API_KEYS and HERMES_API_KEY=mock-key-0123456789ab
uv run poke-hermes-bridge mock-hermes --port 8642 --api-key mock-key-0123456789ab &
uv run poke-hermes-bridge serve &
```

## 2. Tunnel to Poke

```bash
npx poke@latest tunnel http://localhost:8700/mcp -n "Hermes (dev)"
```

Or add the integration by hand at https://poke.com/integrations/new:
Name `Hermes`, URL `https://<your-bridge>/mcp`, API key = a `BRIDGE_API_KEYS` entry.

## 3. Sample prompts to give Poke

- "Ask Hermes to summarise the README of this repo."
- "Start a Hermes task to draft a changelog for v0.2 and ping me when done."
- "Check whether Hermes is healthy." (hermes_health)

## 4. Direct API usage

See `curl.sh` for REST calls and `poke_client.py` for a Python example that
drives a sync ask plus an async task followed over SSE.
`poke-automation-webhook.md` covers the signed inbound webhook.
