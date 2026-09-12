# Setting up Hermes Agent

1. Install Hermes Agent (see https://github.com/NousResearch/hermes-agent and
   https://hermes-agent.nousresearch.com).

2. Enable the API server in `~/.hermes/.env`:

   ```
   API_SERVER_ENABLED=true
   API_SERVER_KEY=<generate with: python -c "import secrets; print(secrets.token_urlsafe(32))">
   ```

   The key must be at least 16 characters. The server binds
   `http://127.0.0.1:8642` by default.

3. Restart Hermes and verify:

   ```bash
   curl -s http://127.0.0.1:8642/health
   curl -s -H "Authorization: Bearer $API_SERVER_KEY" http://127.0.0.1:8642/v1/capabilities
   ```

4. Point the bridge at it:

   ```
   HERMES_BASE_URL=http://127.0.0.1:8642
   HERMES_API_KEY=<same key>
   ```

5. `poke-hermes-bridge check` prints health + capabilities and exits non-zero
   if Hermes is unreachable.
