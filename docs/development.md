# Development

Requires Python >=3.11 and [uv](https://docs.astral.sh/uv/).

```bash
make install   # uv sync --extra dev
make lint      # ruff check + ruff format --check + mypy src
make test      # pytest -q
```

## Layout

- `src/poke_hermes_bridge/app.py` — FastAPI app, REST routes, middleware, MCP mount
- `src/poke_hermes_bridge/service.py` — shared service layer (REST + MCP)
- `src/poke_hermes_bridge/hermes/client.py` — Hermes API client
- `src/poke_hermes_bridge/store.py` — `TaskStore` ABC + in-memory impl
- `src/poke_hermes_bridge/callbacks.py` — poke/webhook delivery, HMAC, SSRF guard
- `src/poke_hermes_bridge/mcp_server.py` — FastMCP tools
- `src/poke_hermes_bridge/devtools/mock_hermes.py` — deterministic mock Hermes
- `src/poke_hermes_bridge/cli.py` — `poke-hermes-bridge` entry point

## Mock Hermes

```bash
uv run poke-hermes-bridge mock-hermes --port 8642 --api-key mock-key-0123456789ab
```

Prompt markers: `[slow]` (3s), `[fail]` (failed run), `[tools]` (tool events).

## Docker

```bash
docker build -t poke-hermes-bridge .
docker compose --profile dev up   # bridge + mock-hermes
```
