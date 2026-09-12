"""Deterministic in-process mock of the Hermes Agent API server.

Canned behaviour driven by prompt markers:
  - echoes the prompt as ``Hermes(mock) received: <prompt>``
  - ``[slow]``   -> the run/chat takes ~3s
  - ``[fail]``   -> the run finishes failed (chat returns 500)
  - ``[tools]``  -> emits tool.started/tool.completed events first
"""

import asyncio
import json
import secrets
import time
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

MOCK_VERSION = "mock-1.0"


def _openai_error(message: str, code: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": "invalid_request_error", "code": code}},
        status_code=status,
    )


def _extract_prompt(body: dict[str, Any]) -> str:
    inp = body.get("input")
    if isinstance(inp, str):
        return inp
    if isinstance(inp, list):
        for msg in reversed(inp):
            if isinstance(msg, dict) and msg.get("role") == "user":
                return str(msg.get("content", ""))
        if inp and isinstance(inp[-1], dict):
            return str(inp[-1].get("content", ""))
    msgs = body.get("messages") or []
    for msg in reversed(msgs):
        if msg.get("role") == "user":
            return str(msg.get("content", ""))
    return ""


class MockRun:
    def __init__(self, run_id: str, prompt: str) -> None:
        self.run_id = run_id
        self.prompt = prompt
        self.status = "queued"
        self.output: str | None = None
        self.error: str | None = None
        self.usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        self.created_at = time.time()
        self.updated_at = time.time()
        self.events: list[dict[str, Any]] = []
        self.done = False
        self.cond = asyncio.Condition()

    def doc(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "object": "hermes.run",
            "run_id": self.run_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "session_id": None,
            "model": "hermes-agent",
        }
        if self.output is not None:
            doc["output"] = self.output
        if self.error is not None:
            doc["error"] = self.error
        doc["usage"] = self.usage
        return doc

    async def emit(self, event: str, **fields: Any) -> None:
        async with self.cond:
            self.events.append(
                {"event": event, "run_id": self.run_id, "timestamp": time.time(), **fields}
            )
            self.cond.notify_all()

    async def finish(
        self, status: str, output: str | None = None, error: str | None = None
    ) -> None:
        async with self.cond:
            self.status = status
            self.output = output
            self.error = error
            self.updated_at = time.time()
            self.done = True
            self.cond.notify_all()


class MockState:
    def __init__(self) -> None:
        self.runs: dict[str, MockRun] = {}
        self.received_session_keys: list[str | None] = []
        self.received_runs: list[dict[str, Any]] = []


def create_mock_app(api_key: str = "mock-key-0123456789ab", run_submission: bool = True) -> FastAPI:
    app = FastAPI(title="mock-hermes")
    app.state.mock = MockState()

    async def require_auth(request: Request) -> None:
        from fastapi import HTTPException

        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {api_key}":
            raise HTTPException(status_code=401)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "platform": "hermes-agent", "version": MOCK_VERSION}

    @app.get("/health/detailed", dependencies=[Depends(require_auth)])
    async def health_detailed() -> dict[str, Any]:
        return {"status": "ok", "platform": "hermes-agent", "version": MOCK_VERSION, "ready": True}

    @app.get("/v1/capabilities", dependencies=[Depends(require_auth)])
    async def capabilities() -> dict[str, Any]:
        return {
            "object": "hermes.api_server.capabilities",
            "platform": "hermes-agent",
            "model": "hermes-agent",
            "auth": {"type": "bearer", "required": True},
            "chat_completions": True,
            "responses_api": True,
            "run_submission": run_submission,
            "run_status": run_submission,
            "run_events_sse": run_submission,
            "run_stop": run_submission,
        }

    def _chat_output(prompt: str) -> str:
        return f"Hermes(mock) received: {prompt}"

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Any:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {api_key}":
            return _openai_error("invalid api key", "invalid_api_key", 401)
        app.state.mock.received_session_keys.append(request.headers.get("x-hermes-session-key"))
        body = await request.json()
        prompt = _extract_prompt(body)
        if "[fail]" in prompt:
            return _openai_error("mock failure", "internal_error", 500)
        if "[slow]" in prompt:
            await asyncio.sleep(3)
        output = _chat_output(prompt)
        if body.get("stream"):

            async def sse() -> Any:
                if "[tools]" in prompt:
                    yield "event: hermes.tool.progress\n"
                    yield 'data: {"tool": "mock_tool", "progress": "running"}\n\n'
                mid = len(output) // 2
                for piece in (output[:mid], output[mid:]):
                    chunk = {
                        "object": "chat.completion.chunk",
                        "choices": [{"delta": {"content": piece}, "index": 0}],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(sse(), media_type="text/event-stream")
        return {
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": output},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    async def _execute(run: MockRun) -> None:
        delay = 3.0 if "[slow]" in run.prompt else 0.05
        if "[tools]" in run.prompt:
            await run.emit("tool.started", tool="mock_tool", preview="mock preview")
            await asyncio.sleep(0.05)
            await run.emit("tool.completed", tool="mock_tool", duration=0.05, error=None)
        await asyncio.sleep(delay)
        if run.done:
            return  # stopped meanwhile
        if "[fail]" in run.prompt:
            await run.emit("run.failed", error="mock failure")
            await run.finish("failed", error="mock failure")
        else:
            await run.emit("run.completed")
            await run.finish("completed", output=_chat_output(run.prompt))

    @app.post("/v1/runs")
    async def create_run(request: Request) -> JSONResponse:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {api_key}":
            return _openai_error("invalid api key", "invalid_api_key", 401)
        body = await request.json()
        app.state.mock.received_runs.append(
            {"input": body.get("input"), "instructions": body.get("instructions")}
        )
        prompt = _extract_prompt(body)
        run = MockRun(f"run_{secrets.token_hex(8)}", prompt)
        run.status = "running"
        app.state.mock.runs[run.run_id] = run
        asyncio.create_task(_execute(run))
        return JSONResponse(
            {"run_id": run.run_id, "status": "queued", "replayed": False}, status_code=202
        )

    def _get_run(run_id: str) -> MockRun | None:
        state: MockState = app.state.mock
        return state.runs.get(run_id)

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str, request: Request) -> Any:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {api_key}":
            return _openai_error("invalid api key", "invalid_api_key", 401)
        run = _get_run(run_id)
        if run is None:
            return _openai_error("run not found", "not_found", 404)
        return run.doc()

    @app.get("/v1/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request) -> Any:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {api_key}":
            return _openai_error("invalid api key", "invalid_api_key", 401)
        run = _get_run(run_id)
        if run is None:
            return _openai_error("run not found", "not_found", 404)

        async def sse() -> Any:
            i = 0
            while True:
                async with run.cond:

                    def _ready(i: int = i) -> bool:
                        return len(run.events) > i or run.done

                    try:
                        await asyncio.wait_for(run.cond.wait_for(_ready), timeout=15.0)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    pending = list(run.events[i:])
                    i = len(run.events)
                    done = run.done
                for ev in pending:
                    yield f"data: {json.dumps(ev)}\n\n"
                if done and not pending:
                    yield ": stream closed\n\n"
                    return
                if done:
                    yield ": stream closed\n\n"
                    return

        return StreamingResponse(sse(), media_type="text/event-stream")

    @app.post("/v1/runs/{run_id}/stop")
    async def stop_run(run_id: str, request: Request) -> Any:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {api_key}":
            return _openai_error("invalid api key", "invalid_api_key", 401)
        run = _get_run(run_id)
        if run is None:
            return _openai_error("run not found", "not_found", 404)
        if run.done:
            return _openai_error("run already finished", "run_not_active", 409)
        await run.finish("cancelled", error="cancelled by user")
        return {"run_id": run_id, "status": "stopping"}

    @app.post("/v1/runs/{run_id}/steer")
    async def steer_run(run_id: str, request: Request) -> Any:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {api_key}":
            return _openai_error("invalid api key", "invalid_api_key", 401)
        run = _get_run(run_id)
        if run is None:
            return _openai_error("run not found", "not_found", 404)
        if run.done or run.status not in ("running", "queued"):
            return _openai_error("run not accepting steer", "run_not_accepting_steer", 409)
        await request.json()
        return {"object": "hermes.run.steer", "accepted": True}

    @app.post("/v1/runs/{run_id}/approval")
    async def approval(run_id: str, request: Request) -> Any:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {api_key}":
            return _openai_error("invalid api key", "invalid_api_key", 401)
        run = _get_run(run_id)
        if run is None:
            return _openai_error("run not found", "not_found", 404)
        await request.json()
        return {"object": "hermes.run.approval", "accepted": True}

    return app


def run_mock(host: str, port: int, api_key: str) -> None:
    import uvicorn

    uvicorn.run(create_mock_app(api_key=api_key), host=host, port=port)
