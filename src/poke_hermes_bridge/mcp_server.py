"""MCP server mounted at /mcp for Poke integrations.

Tools are thin wrappers over :class:`BridgeService`; tasks created through MCP
are owned by the ``mcp`` caller, so ``hermes_task_status``/``hermes_stop_task``/
``hermes_steer_task`` only see MCP-created tasks.
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import Settings
from .errors import BridgeError
from .models import PokeCallback, TaskCreate
from .service import BridgeService

MCP_CALLER = "mcp"


def create_mcp_server(service: BridgeService, settings: Settings) -> FastMCP:
    server: FastMCP = FastMCP(
        "poke-hermes-bridge",
        host=settings.bridge_host,
        port=settings.bridge_port,
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
    )

    @server.tool()
    async def hermes_ask(
        prompt: str,
        conversation_id: str | None = None,
        instructions: str | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        """Ask Hermes a question and wait for the answer (synchronous).

        Use this for quick questions and summaries that finish within the
        timeout. For long-running work use hermes_start_task instead.

        Args:
            prompt: The user prompt to send to Hermes.
            conversation_id: Optional stable ID to keep Hermes memory scoped to
                one conversation across calls (letters, digits, . _ : -).
            instructions: Optional system-prompt instructions for Hermes.
            timeout_seconds: Max seconds to wait (capped server-side).

        Returns the agent's answer text, or a string starting with "error:".
        """
        spec = TaskCreate(
            prompt=prompt,
            instructions=instructions,
            conversation_id=conversation_id,
            mode="sync",
            timeout_seconds=timeout_seconds,
        )
        try:
            task, _ = await service.submit(spec, caller=MCP_CALLER)
        except BridgeError as exc:
            return f"error: {exc.code}: {exc.message}"
        return task.output or ""

    @server.tool()
    async def hermes_start_task(
        prompt: str,
        conversation_id: str | None = None,
        instructions: str | None = None,
        notify_poke: bool = True,
    ) -> dict[str, Any]:
        """Start a long-running Hermes task in the background (asynchronous).

        Use this for anything that may take a while (research, coding, tool
        use). Returns immediately with a task id; check progress with
        hermes_task_status. If notify_poke is true and the bridge is configured
        with a Poke API key, the result is pushed back to Poke automatically
        when the task finishes.

        Args:
            prompt: The task prompt for Hermes.
            conversation_id: Optional stable conversation scope ID.
            instructions: Optional system-prompt instructions.
            notify_poke: Push the final result to Poke when done.
        """
        callback = PokeCallback() if (notify_poke and settings.poke_api_key) else None
        spec = TaskCreate(
            prompt=prompt,
            instructions=instructions,
            conversation_id=conversation_id,
            mode="async",
            callback=callback,
        )
        try:
            task, _ = await service.submit(spec, caller=MCP_CALLER)
        except BridgeError as exc:
            return {"error": f"{exc.code}: {exc.message}"}
        return {
            "task_id": task.id,
            "status": task.status,
            "check": f"call hermes_task_status('{task.id}') to poll for the result",
            "notify_poke": callback is not None,
        }

    @server.tool()
    async def hermes_task_status(task_id: str) -> dict[str, Any]:
        """Check the status of a task started with hermes_start_task.

        Returns the task record including status (queued/running/
        waiting_for_approval/stopping/completed/failed/cancelled), and the
        output once completed.
        """
        task = await service.store.get(task_id, owner=MCP_CALLER)
        if task is None:
            return {"error": "task_not_found"}
        return task.model_dump(mode="json")

    @server.tool()
    async def hermes_stop_task(task_id: str) -> dict[str, Any]:
        """Ask Hermes to stop a running task started with hermes_start_task."""
        try:
            task = await service.stop(task_id, MCP_CALLER)
        except BridgeError as exc:
            return {"error": f"{exc.code}: {exc.message}"}
        return {"task_id": task.id, "status": task.status}

    @server.tool()
    async def hermes_steer_task(task_id: str, input: str) -> dict[str, Any]:
        """Send steering input to a running Hermes task (only while running)."""
        try:
            return await service.steer(task_id, MCP_CALLER, _Steer(input=input))
        except BridgeError as exc:
            return {"error": f"{exc.code}: {exc.message}"}

    @server.tool()
    async def hermes_health() -> dict[str, Any]:
        """Check whether the Hermes Agent API server is reachable."""
        try:
            h = await service.hermes.health()
            caps = await service.hermes_capabilities()
        except Exception as exc:  # noqa: BLE001
            return {"reachable": False, "error": str(exc)}
        return {"reachable": True, "version": h.get("version"), "capabilities": caps}

    return server


class _Steer:
    def __init__(self, input: str) -> None:
        self.input = input
