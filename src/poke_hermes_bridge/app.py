"""FastAPI application: REST bridge protocol + mounted MCP server for Poke."""

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .auth import authenticate
from .callbacks import ReplayCache, verify_inbound_signature
from .config import Settings
from .errors import BridgeError, envelope
from .hermes.client import HermesClient, HermesError
from .models import ApprovalRequest, SteerRequest, TaskCreate
from .ratelimit import RateLimiter
from .service import BridgeService
from .sse import format_sse

logger = logging.getLogger("poke_hermes_bridge")
PROTOCOL_VERSION = "poke-hermes-bridge/1"


class BearerASGIMiddleware:
    """Pure-ASGI bearer check for the mounted MCP app."""

    def __init__(self, app: ASGIApp, keys: dict[str, str]) -> None:
        self.app = app
        self.keys = keys

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
            if authenticate(headers.get("authorization"), self.keys) is None:
                response = JSONResponse(
                    {"error": {"code": "unauthorized", "message": "invalid or missing API key"}},
                    status_code=401,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(
    settings: Settings | None = None,
    hermes: HermesClient | None = None,
    service: BridgeService | None = None,
) -> FastAPI:
    settings = settings or Settings()
    problems = settings.validate_startup()
    if problems:
        raise RuntimeError("invalid bridge configuration: " + "; ".join(problems))
    logging.basicConfig(level=settings.bridge_log_level.upper())

    if settings.bridge_host not in ("127.0.0.1", "::1", "localhost"):
        logger.warning(
            "BRIDGE_HOST=%s is non-loopback; terminate TLS in front of the bridge",
            settings.bridge_host,
        )

    hermes = hermes or HermesClient(
        base_url=settings.hermes_base_url,
        api_key=settings.hermes_api_key,
        timeout=settings.hermes_timeout_seconds,
        verify_tls=settings.hermes_verify_tls,
    )
    service = service or BridgeService(settings, hermes)
    limiter = RateLimiter(settings.bridge_rate_limit_per_minute)
    replay_cache = ReplayCache()

    # -- MCP sub-app -----------------------------------------------------
    from .mcp_server import create_mcp_server

    mcp_server = create_mcp_server(service, settings)
    mcp_inner = mcp_server.streamable_http_app()
    mcp_app: ASGIApp = BearerASGIMiddleware(mcp_inner, settings.api_keys())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Mounted apps don't run their own lifespan; run the MCP session manager's.
        async with mcp_inner.router.lifespan_context(mcp_inner):
            yield

    app = FastAPI(
        title="poke-hermes-bridge",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.service = service
    app.state.settings = settings

    if origins := settings.cors_origin_list():
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.mount("/mcp", mcp_app)

    # -- middleware ------------------------------------------------------

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Any]]
    ) -> Any:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        start = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        logger.info(
            "%s %s -> %d (%.0fms) req=%s",
            request.method,
            request.url.path,
            response.status_code,
            (time.monotonic() - start) * 1000,
            request_id,
        )
        return response

    @app.exception_handler(BridgeError)
    async def bridge_error_handler(request: Request, exc: BridgeError) -> JSONResponse:
        resp = JSONResponse(
            envelope(exc.code, exc.message, getattr(request.state, "request_id", "-")),
            status_code=exc.status,
        )
        if exc.code == "rate_limited" and getattr(exc, "retry_after", None):
            resp.headers["Retry-After"] = str(exc.retry_after)  # type: ignore[attr-defined]
        return resp

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            envelope(
                "invalid_request",
                "request failed validation",
                getattr(request.state, "request_id", "-"),
            ),
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def generic_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return JSONResponse(
            envelope(
                "internal_error",
                "internal error",
                getattr(request.state, "request_id", "-"),
            ),
            status_code=500,
        )

    # -- auth / rate limit dependencies -----------------------------------

    async def require_caller(request: Request) -> str:
        name = authenticate(request.headers.get("authorization"), settings.api_keys())
        if name is None:
            raise BridgeError("unauthorized", "invalid or missing API key", 401)
        allowed, retry = limiter.allow(name)
        if not allowed:
            err = BridgeError("rate_limited", "rate limit exceeded", 429)
            err.retry_after = f"{retry:.0f}"  # type: ignore[attr-defined]
            raise err
        request.state.caller = name
        return name

    # -- routes -------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "poke-hermes-bridge",
            "version": __version__,
        }

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        try:
            h = await hermes.health()
        except HermesError as exc:
            return JSONResponse(
                {
                    "status": "degraded",
                    "hermes": {"reachable": False, "error": str(exc)},
                },
                status_code=503,
            )
        return JSONResponse(
            {
                "status": "ready",
                "hermes": {"reachable": True, "version": h.get("version")},
            }
        )

    @app.get("/v1/capabilities")
    async def capabilities(caller: str = Depends(require_caller)) -> dict[str, Any]:
        return {
            "service": "poke-hermes-bridge",
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "features": {
                "sync": True,
                "async": True,
                "streaming": True,
                "callbacks": True,
                "mcp": True,
            },
            "hermes": await service.hermes_capabilities(),
        }

    @app.post("/v1/tasks", status_code=200)
    async def create_task(
        spec: TaskCreate,
        request: Request,
        caller: str = Depends(require_caller),
    ) -> JSONResponse:
        idem = request.headers.get("idempotency-key")
        task, created = await service.submit(spec, caller, idempotency_key=idem)
        status = 202 if task.mode == "async" and created else 200
        return JSONResponse(task.model_dump(mode="json"), status_code=status)

    @app.get("/v1/tasks/{task_id}")
    async def get_task(task_id: str, caller: str = Depends(require_caller)) -> dict[str, Any]:
        task = await service.store.get(task_id, owner=caller)
        if task is None:
            raise BridgeError("task_not_found", "unknown task", 404)
        return task.model_dump(mode="json")

    @app.get("/v1/tasks/{task_id}/events")
    async def task_events(
        task_id: str, request: Request, caller: str = Depends(require_caller)
    ) -> StreamingResponse:
        task = await service.store.get(task_id, owner=caller)
        if task is None:
            raise BridgeError("task_not_found", "unknown task", 404)
        last_id = 0
        if header := request.headers.get("last-event-id"):
            try:
                last_id = int(header)
            except ValueError:
                last_id = 0

        async def stream() -> AsyncIterator[str]:
            seq = last_id
            for ev in await service.store.events_since(task_id, seq):
                seq = max(seq, ev.seq)
                yield format_sse(json.dumps(ev.data), event=ev.name, event_id=ev.seq)
            if task.status in ("completed", "failed", "cancelled"):
                return
            async with contextlib.aclosing(service.store.subscribe(task_id)) as agen:
                while True:
                    try:
                        ev = await asyncio.wait_for(agen.__anext__(), timeout=15.0)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    except StopAsyncIteration:
                        return
                    if ev.seq <= seq:
                        continue
                    seq = ev.seq
                    yield format_sse(json.dumps(ev.data), event=ev.name, event_id=ev.seq)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/v1/tasks/{task_id}/stop")
    async def stop_task(task_id: str, caller: str = Depends(require_caller)) -> dict[str, Any]:
        task = await service.stop(task_id, caller)
        return task.model_dump(mode="json")

    @app.post("/v1/tasks/{task_id}/steer")
    async def steer_task(
        task_id: str, req: SteerRequest, caller: str = Depends(require_caller)
    ) -> dict[str, Any]:
        return await service.steer(task_id, caller, req)

    @app.post("/v1/tasks/{task_id}/approval")
    async def approve_task(
        task_id: str, req: ApprovalRequest, caller: str = Depends(require_caller)
    ) -> dict[str, Any]:
        return await service.approve(task_id, caller, req)

    @app.post("/v1/webhooks/poke")
    async def poke_webhook(request: Request) -> JSONResponse:
        if not settings.bridge_webhook_secret:
            raise BridgeError("not_found", "not found", 404)
        timestamp = request.headers.get("x-bridge-timestamp", "")
        signature = request.headers.get("x-bridge-signature", "")
        raw = await request.body()
        if not verify_inbound_signature(settings.bridge_webhook_secret, timestamp, raw, signature):
            raise BridgeError("invalid_signature", "signature verification failed", 401)
        if not replay_cache.check_and_add(f"{timestamp}:{signature}"):
            raise BridgeError("replay_detected", "duplicate webhook delivery", 409)
        try:
            spec = TaskCreate.model_validate_json(raw)
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(
                "invalid_request", f"body is not a valid TaskCreate: {exc}", 422
            ) from exc
        if spec.callback is None and settings.poke_api_key:
            from .models import PokeCallback

            spec = spec.model_copy(update={"callback": PokeCallback()})
        task, _ = await service.submit(spec, caller="webhook", force_async=True)
        return JSONResponse(task.model_dump(mode="json"), status_code=202)

    return app
