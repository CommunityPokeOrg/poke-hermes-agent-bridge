# Setting up Poke

## 1. Expose the bridge

Local dev:

```bash
npx poke@latest tunnel http://localhost:8700/mcp -n "Hermes (dev)"
```

Production: put the bridge behind TLS and note its public URL
(`BRIDGE_PUBLIC_URL`). `poke-hermes-bridge poke-link` prints a prefilled
integration link.

## 2. Add the MCP integration

- Dashboard: https://poke.com/integrations/new — Name `Hermes`, MCP Server URL
  `https://<your-bridge>/mcp`, API Key: one of `BRIDGE_API_KEYS`.
- CLI: `npx poke@latest mcp add https://<your-bridge>/mcp -n "Hermes" -k <key>`

Poke sends the key as `Authorization: Bearer`; the same keys protect `/mcp`.

## 3. Result push-backs (optional)

Create a V2 API key in Poke (Kitchen) and set `POKE_API_KEY`. Async tasks
started with a `{"type":"poke"}` callback (the default for the MCP
`hermes_start_task` tool and for inbound webhooks) POST their result to
`https://poke.com/api/v1/inbound/api-message`.

## 4. Try it

- "Ask Hermes to summarise today's AI news" -> `hermes_ask`
- "Start a Hermes task to research X and tell me when it's done" ->
  `hermes_start_task` + callback
