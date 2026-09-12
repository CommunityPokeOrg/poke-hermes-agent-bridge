"""Task storage.

``TaskStore`` is the persistence seam: a durable implementation (Redis, SQLite,
Postgres) can replace :class:`InMemoryTaskStore` later. The in-memory
implementation is single-process — tasks do not survive restarts and are not
shared between workers.
"""

import asyncio
import secrets
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any

from .models import TERMINAL_STATUSES, Task


class StoredEvent:
    __slots__ = ("seq", "name", "data")

    def __init__(self, seq: int, name: str, data: dict[str, Any]) -> None:
        self.seq = seq
        self.name = name
        self.data = data


class TaskRecord:
    """Internal record: the public Task plus bookkeeping."""

    def __init__(self, task: Task, owner: str) -> None:
        self.task = task
        self.owner = owner
        self.events: list[StoredEvent] = []
        self.next_seq = 1
        self.subscribers: list[asyncio.Queue[StoredEvent | None]] = []
        self.truncated = False


class TaskStore(ABC):
    """Interface a persistent store can implement later."""

    @abstractmethod
    async def create(self, task: Task, owner: str) -> Task: ...

    @abstractmethod
    async def get(self, task_id: str, owner: str | None = None) -> Task | None: ...

    @abstractmethod
    async def update(self, task: Task) -> None: ...

    @abstractmethod
    async def append_event(self, task_id: str, name: str, data: dict[str, Any]) -> int: ...

    @abstractmethod
    async def events_since(self, task_id: str, after_seq: int) -> list[StoredEvent]: ...

    @abstractmethod
    def subscribe(self, task_id: str) -> AsyncGenerator[StoredEvent, None]: ...

    @abstractmethod
    async def notify_terminal(self, task_id: str) -> None: ...

    @abstractmethod
    async def count_active(self) -> int: ...

    @abstractmethod
    async def idempotency_lookup(
        self, caller: str, key: str, body_hash: str
    ) -> tuple[str | None, bool]: ...

    @abstractmethod
    async def idempotency_store(
        self, caller: str, key: str, body_hash: str, task_id: str
    ) -> None: ...


class InMemoryTaskStore(TaskStore):
    def __init__(self, ttl_seconds: float = 3600.0, max_events_per_task: int = 500) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_events = max_events_per_task
        self._records: dict[str, TaskRecord] = {}
        self._idempotency: dict[tuple[str, str], tuple[str, str, float]] = {}
        self._lock = asyncio.Lock()

    # -- tasks -----------------------------------------------------------

    async def create(self, task: Task, owner: str) -> Task:
        async with self._lock:
            self._evict_expired()
            self._records[task.id] = TaskRecord(task, owner)
        await self.append_event(task.id, "task.created", {"task_id": task.id, "mode": task.mode})
        return task

    async def get(self, task_id: str, owner: str | None = None) -> Task | None:
        async with self._lock:
            self._evict_expired()
            rec = self._records.get(task_id)
            if rec is None or (owner is not None and rec.owner != owner):
                return None
            return rec.task

    async def update(self, task: Task) -> None:
        task.updated_at = time.time()
        async with self._lock:
            rec = self._records.get(task.id)
            if rec is not None:
                rec.task = task

    async def count_active(self) -> int:
        async with self._lock:
            self._evict_expired()
            return sum(1 for r in self._records.values() if r.task.status not in TERMINAL_STATUSES)

    # -- events ------------------------------------------------------------

    async def append_event(self, task_id: str, name: str, data: dict[str, Any]) -> int:
        async with self._lock:
            rec = self._records.get(task_id)
            if rec is None:
                return -1
            ev = StoredEvent(rec.next_seq, name, data)
            rec.next_seq += 1
            rec.events.append(ev)
            if len(rec.events) > self.max_events:
                drop = len(rec.events) - self.max_events
                del rec.events[:drop]
                if not rec.truncated:
                    rec.truncated = True
                    marker = StoredEvent(rec.next_seq, "task.events_truncated", {})
                    rec.next_seq += 1
                    rec.events.insert(0, marker)
            subs = list(rec.subscribers)
        for q in subs:
            q.put_nowait(ev)
        return ev.seq

    async def events_since(self, task_id: str, after_seq: int) -> list[StoredEvent]:
        async with self._lock:
            rec = self._records.get(task_id)
            if rec is None:
                return []
            return [e for e in rec.events if e.seq > after_seq]

    async def subscribe(self, task_id: str) -> AsyncGenerator[StoredEvent, None]:
        queue: asyncio.Queue[StoredEvent | None] = asyncio.Queue(maxsize=1000)
        async with self._lock:
            rec = self._records.get(task_id)
            if rec is None:
                return
            rec.subscribers.append(queue)
            terminal = rec.task.status in TERMINAL_STATUSES
        if terminal:
            queue.put_nowait(None)
        try:
            while True:
                ev = await queue.get()
                if ev is None:
                    return
                yield ev
        finally:
            async with self._lock:
                rec = self._records.get(task_id)
                if rec is not None and queue in rec.subscribers:
                    rec.subscribers.remove(queue)

    async def notify_terminal(self, task_id: str) -> None:
        """Wake subscribers so they exit once the task is terminal."""
        async with self._lock:
            rec = self._records.get(task_id)
            if rec is None:
                return
            subs = list(rec.subscribers)
        for q in subs:
            q.put_nowait(None)

    # -- idempotency -------------------------------------------------------

    async def idempotency_lookup(
        self, caller: str, key: str, body_hash: str
    ) -> tuple[str | None, bool]:
        """Returns (task_id_or_None, conflict)."""
        async with self._lock:
            entry = self._idempotency.get((caller, key))
            if entry is None:
                return None, False
            stored_hash, task_id, created = entry
            if time.time() - created > self.ttl_seconds:
                del self._idempotency[(caller, key)]
                return None, False
            if stored_hash != body_hash:
                return None, True
            return task_id, False

    async def idempotency_store(self, caller: str, key: str, body_hash: str, task_id: str) -> None:
        async with self._lock:
            self._idempotency[(caller, key)] = (body_hash, task_id, time.time())

    # -- internals ---------------------------------------------------------

    def _evict_expired(self) -> None:
        cutoff = time.time() - self.ttl_seconds
        expired = [tid for tid, r in self._records.items() if r.task.created_at < cutoff]
        for tid in expired:
            rec = self._records.pop(tid)
            for q in rec.subscribers:
                q.put_nowait(None)


def new_task_id() -> str:
    return f"task_{secrets.token_hex(12)}"
