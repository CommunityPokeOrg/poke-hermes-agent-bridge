"""Service layer shared by the REST API and the MCP tools."""

import asyncio
import logging
import time
from typing import Any

from .callbacks import CallbackDispatcher, body_hash
from .config import Settings
from .errors import BridgeError
from .hermes.client import (
    HermesAuthError,
    HermesClient,
    HermesError,
    HermesRejected,
    HermesTimeout,
    HermesUnavailable,
)
from .models import (
    TERMINAL_STATUSES,
    ApprovalRequest,
    CallbackStatus,
    PokeCallback,
    Task,
    TaskCreate,
    Usage,
    WebhookCallback,
)
from .store import InMemoryTaskStore, TaskStore, new_task_id

logger = logging.getLogger("poke_hermes_bridge.service")

CAPABILITIES_CACHE_TTL = 60.0


def session_key_for(caller: str, conversation_id: str | None) -> str | None:
    if not conversation_id:
        return None
    return f"poke:{caller}:{conversation_id}"


class BridgeService:
    def __init__(
        self,
        settings: Settings,
        hermes: HermesClient,
        store: TaskStore | None = None,
        callbacks: CallbackDispatcher | None = None,
    ) -> None:
        self.settings = settings
        self.hermes = hermes
        self.store: TaskStore = store or InMemoryTaskStore(
            ttl_seconds=settings.bridge_task_ttl_seconds,
            max_events_per_task=settings.bridge_max_events_per_task,
        )
        self.callbacks = callbacks or CallbackDispatcher(settings)
        self._caps_cache: tuple[float, dict[str, Any] | None] = (0.0, None)
        self._background: set[asyncio.Task[Any]] = set()

    # -- Hermes capabilities (lazy, 60s cache) ------------------------------

    async def hermes_capabilities(self) -> dict[str, Any] | None:
        cached_at, cached = self._caps_cache
        if time.monotonic() - cached_at < CAPABILITIES_CACHE_TTL:
            return cached
        try:
            caps = await self.hermes.capabilities()
        except HermesError:
            caps = None
        self._caps_cache = (time.monotonic(), caps)
        return caps

    # -- task submission -----------------------------------------------------

    def _check_capacity(self, active: int) -> None:
        if active >= self.settings.bridge_max_concurrent_tasks:
            raise BridgeError(
                "too_many_active_tasks",
                f"concurrency cap of {self.settings.bridge_max_concurrent_tasks} reached",
                status=429,
            )

    async def submit(
        self,
        spec: TaskCreate,
        caller: str,
        idempotency_key: str | None = None,
        force_async: bool = False,
    ) -> tuple[Task, bool]:
        """Create a task. Returns (task, created): created=False means an
        idempotent replay returned an existing task."""
        if force_async:
            spec = spec.model_copy(update={"mode": "async"})
        if len(spec.prompt) > self.settings.bridge_max_prompt_chars:
            raise BridgeError(
                "prompt_too_long",
                f"prompt exceeds {self.settings.bridge_max_prompt_chars} characters",
                status=422,
            )
        timeout = spec.timeout_seconds or self.settings.bridge_sync_timeout_seconds
        timeout = min(timeout, self.settings.bridge_max_timeout_seconds)
        body = body_hash(spec.model_dump_json().encode())

        if idempotency_key:
            task_id, conflict = await self.store.idempotency_lookup(caller, idempotency_key, body)
            if conflict:
                raise BridgeError(
                    "idempotency_conflict",
                    "Idempotency-Key was already used with a different body",
                    status=409,
                )
            if task_id:
                existing = await self.store.get(task_id, owner=caller)
                if existing is not None:
                    return existing, False

        if spec.mode == "async":
            self._check_capacity(await self.store.count_active())

        task = Task(
            id=new_task_id(),
            status="queued",
            mode=spec.mode,
            conversation_id=spec.conversation_id,
            created_at=time.time(),
            updated_at=time.time(),
            metadata=spec.metadata,
            callback=CallbackStatus(type=spec.callback.type) if spec.callback else None,
        )
        await self.store.create(task, owner=caller)
        if idempotency_key:
            await self.store.idempotency_store(caller, idempotency_key, body, task.id)

        if spec.mode == "sync":
            await self._run_sync(task, spec, caller, timeout)
            return task, True

        self._spawn(self._run_async(task, spec, caller))
        return task, True

    # -- sync mode -----------------------------------------------------------

    def _messages(self, spec: TaskCreate) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = []
        if spec.instructions:
            msgs.append({"role": "system", "content": spec.instructions})
        msgs.append({"role": "user", "content": spec.prompt})
        return msgs

    async def _run_sync(
        self, spec_task: Task, spec: TaskCreate, caller: str, timeout: float
    ) -> None:
        task = spec_task
        task.status = "running"
        await self.store.update(task)
        try:
            resp = await asyncio.wait_for(
                self.hermes.chat(
                    self._messages(spec),
                    session_key=session_key_for(caller, spec.conversation_id),
                    model=self.settings.hermes_model,
                    timeout=timeout,
                ),
                timeout=timeout,
            )
        except HermesTimeout as exc:
            await self._fail(task, "hermes timeout")
            raise BridgeError("hermes_timeout", "hermes did not respond in time", 504) from exc
        except TimeoutError as exc:
            await self._fail(task, "hermes timeout")
            raise BridgeError("hermes_timeout", "hermes did not respond in time", 504) from exc
        except HermesAuthError as exc:
            await self._fail(task, "hermes auth failed")
            raise BridgeError(
                "hermes_auth_failed", "hermes rejected the configured API key", 502
            ) from exc
        except HermesUnavailable as exc:
            await self._fail(task, "hermes unavailable")
            raise BridgeError("hermes_unavailable", str(exc), 502) from exc
        except HermesRejected as exc:
            await self._fail(task, f"hermes rejected: {exc.message}")
            raise BridgeError("hermes_error", f"hermes error: {exc.message}", 502) from exc
        choice = (resp.get("choices") or [{}])[0]
        task.output = (choice.get("message") or {}).get("content") or ""
        usage = resp.get("usage") or {}
        task.usage = Usage(
            input_tokens=usage.get("prompt_tokens", usage.get("input_tokens", 0)),
            output_tokens=usage.get("completion_tokens", usage.get("output_tokens", 0)),
            total_tokens=usage.get("total_tokens", 0),
        )
        task.status = "completed"
        await self.store.update(task)
        await self.store.append_event(
            task.id,
            "task.completed",
            {"output": task.output, "usage": task.usage.model_dump()},
        )
        await self.store.notify_terminal(task.id)
        await self._maybe_callback(task, spec)

    # -- async mode ----------------------------------------------------------

    def _spawn(self, coro: Any) -> None:
        t = asyncio.create_task(coro)
        self._background.add(t)
        t.add_done_callback(self._background.discard)

    async def _run_async(self, task: Task, spec: TaskCreate, caller: str) -> None:
        try:
            caps = await self.hermes_capabilities()
            if caps and caps.get("run_submission"):
                await self._run_via_runs(task, spec, caller)
            else:
                await self._run_via_chat(task, spec, caller)
        except Exception as exc:  # noqa: BLE001
            logger.exception("async task %s failed", task.id)
            await self._fail(task, f"internal error: {type(exc).__name__}")
            await self._maybe_callback(task, spec)

    async def _run_via_runs(self, task: Task, spec: TaskCreate, caller: str) -> None:
        try:
            resp = await self.hermes.create_run(
                input=self._messages(spec),
                instructions=spec.instructions,
                session_key=session_key_for(caller, spec.conversation_id),
                model=self.settings.hermes_model,
            )
        except HermesError as exc:
            await self._fail(task, f"run submission failed: {_safe(exc)}")
            await self._maybe_callback(task, spec)
            return
        task.hermes_run_id = resp.get("run_id")
        task.status = "running"
        await self.store.update(task)
        await self.store.append_event(task.id, "task.status", {"status": "running"})
        asyncio.create_task(self._follow_run(task, spec))

    async def _run_via_chat(self, task: Task, spec: TaskCreate, caller: str) -> None:
        task.status = "running"
        await self.store.update(task)
        await self.store.append_event(task.id, "task.status", {"status": "running"})
        try:
            resp = await self.hermes.chat(
                self._messages(spec),
                session_key=session_key_for(caller, spec.conversation_id),
                model=self.settings.hermes_model,
            )
        except HermesError as exc:
            await self._fail(task, f"chat failed: {_safe(exc)}")
            await self._maybe_callback(task, spec)
            return
        if task.status in TERMINAL_STATUSES:
            return  # e.g. cancelled meanwhile
        choice = (resp.get("choices") or [{}])[0]
        task.output = (choice.get("message") or {}).get("content") or ""
        task.status = "completed"
        await self.store.update(task)
        await self.store.append_event(task.id, "task.completed", {"output": task.output})
        await self.store.notify_terminal(task.id)
        await self._maybe_callback(task, spec)

    async def _follow_run(self, task: Task, spec: TaskCreate) -> None:
        if task.hermes_run_id is None:
            return
        run_id = task.hermes_run_id
        poll_interval = self.settings.bridge_poll_interval_seconds
        stream_done = asyncio.Event()

        async def consume() -> None:
            try:
                async for payload in self.hermes.stream_run_events(run_id):
                    name = str(payload.get("event", "unknown"))
                    await self.store.append_event(task.id, f"hermes.{name}", payload)
            except HermesError as exc:
                logger.info("run %s event stream ended with %s", run_id, _safe(exc))
            finally:
                stream_done.set()

        async def poll() -> None:
            while not stream_done.is_set():
                await asyncio.sleep(poll_interval)
                if stream_done.is_set():
                    return
                try:
                    run = await self.hermes.get_run(run_id)
                except HermesError:
                    continue
                await self._apply_run_status(task, run)
                if task.status in TERMINAL_STATUSES:
                    return

        consumer = asyncio.create_task(consume())
        poller = asyncio.create_task(poll())
        # Wait for the stream to close, then poll until terminal.
        await stream_done.wait()
        poller.cancel()
        while task.status not in TERMINAL_STATUSES:
            try:
                run = await self.hermes.get_run(run_id)
                await self._apply_run_status(task, run)
            except HermesError as exc:
                logger.info("run %s poll failed: %s", run_id, _safe(exc))
            if task.status not in TERMINAL_STATUSES:
                await asyncio.sleep(poll_interval)
        await consumer
        await self.store.notify_terminal(task.id)
        await self._maybe_callback(task, spec)

    async def _apply_run_status(self, task: Task, run: dict[str, Any]) -> None:
        status = str(run.get("status", ""))
        if status not in TERMINAL_STATUSES:
            if (
                status
                and status != task.status
                and status
                in (
                    "queued",
                    "running",
                    "waiting_for_approval",
                    "stopping",
                )
            ):
                task.status = status  # type: ignore[assignment]
                await self.store.update(task)
                await self.store.append_event(task.id, "task.status", {"status": status})
            return
        if task.status in TERMINAL_STATUSES:
            return
        task.status = status  # type: ignore[assignment]
        if status == "completed":
            task.output = run.get("output")
            usage = run.get("usage") or {}
            task.usage = Usage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            )
        elif status in ("failed", "cancelled"):
            task.error = run.get("error") or status
        await self.store.update(task)
        if status == "completed":
            await self.store.append_event(
                task.id,
                "task.completed",
                {"output": task.output, "usage": task.usage.model_dump() if task.usage else None},
            )
        else:
            await self.store.append_event(task.id, "task.failed", {"error": task.error})

    # -- control -------------------------------------------------------------

    async def _get_active(self, task_id: str, owner: str) -> Task:
        task = await self.store.get(task_id, owner=owner)
        if task is None:
            raise BridgeError("task_not_found", "unknown task", 404)
        if task.status in TERMINAL_STATUSES:
            raise BridgeError("task_not_active", f"task is {task.status}", 409)
        return task

    async def stop(self, task_id: str, owner: str) -> Task:
        task = await self._get_active(task_id, owner)
        if task.hermes_run_id:
            try:
                await self.hermes.stop_run(task.hermes_run_id)
            except HermesRejected as exc:
                raise BridgeError("hermes_error", f"hermes error: {exc.message}", 502) from exc
            except HermesError as exc:
                raise BridgeError("hermes_unavailable", _safe(exc), 502) from exc
            task.status = "stopping"
            await self.store.update(task)
            await self.store.append_event(task.id, "task.status", {"status": "stopping"})
        else:
            task.status = "cancelled"
            task.error = "cancelled"
            await self.store.update(task)
            await self.store.append_event(task.id, "task.failed", {"error": "cancelled"})
            await self.store.notify_terminal(task.id)
        return task

    async def steer(self, task_id: str, owner: str, req: Any) -> dict[str, Any]:
        task = await self._get_active(task_id, owner)
        if not task.hermes_run_id:
            raise BridgeError("task_not_accepting_steer", "task has no hermes run", 409)
        try:
            return await self.hermes.steer_run(task.hermes_run_id, req.input)
        except HermesRejected as exc:
            if exc.status == 409:
                raise BridgeError("task_not_accepting_steer", exc.message, 409) from exc
            raise BridgeError("hermes_error", f"hermes error: {exc.message}", 502) from exc
        except HermesError as exc:
            raise BridgeError("hermes_unavailable", _safe(exc), 502) from exc

    async def approve(self, task_id: str, owner: str, req: ApprovalRequest) -> dict[str, Any]:
        task = await self._get_active(task_id, owner)
        if not task.hermes_run_id:
            raise BridgeError("task_not_active", "task has no hermes run", 409)
        try:
            return await self.hermes.resolve_approval(
                task.hermes_run_id, req.choice, req.request_id
            )
        except HermesRejected as exc:
            raise BridgeError("hermes_error", f"hermes error: {exc.message}", 502) from exc
        except HermesError as exc:
            raise BridgeError("hermes_unavailable", _safe(exc), 502) from exc

    # -- helpers ---------------------------------------------------------------

    async def _fail(self, task: Task, error: str) -> None:
        task.status = "failed"
        task.error = error
        await self.store.update(task)
        await self.store.append_event(task.id, "task.failed", {"error": error})
        await self.store.notify_terminal(task.id)

    async def _maybe_callback(self, task: Task, spec: TaskCreate) -> None:
        if task.status not in TERMINAL_STATUSES or spec.callback is None:
            return
        cb = spec.callback
        if isinstance(cb, PokeCallback | WebhookCallback):
            await self.callbacks.deliver(task, cb)
            await self.store.update(task)


def _safe(exc: Exception) -> str:
    """Exception text safe to surface (clients never carry the Hermes key)."""
    return f"{type(exc).__name__}: {exc}"
