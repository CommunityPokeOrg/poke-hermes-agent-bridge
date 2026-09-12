"""Terminal-state callbacks: Poke inbound API and signed webhooks."""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
from typing import Any

import httpx

from .config import Settings
from .models import PokeCallback, Task, WebhookCallback

logger = logging.getLogger("poke_hermes_bridge.callbacks")

# Patchable in tests; production backoff: 1s, 4s, 16s.
CALLBACK_BACKOFF_SECONDS = (1.0, 4.0, 16.0)


async def check_callback_url(url: str, allow_insecure: bool) -> str | None:
    """SSRF guard. Returns a rejection reason, or None if the URL is allowed."""
    if allow_insecure:
        return None  # operator opted out of scheme/SSRF checks
    parsed = httpx.URL(url)
    if parsed.scheme != "https":
        return f"callback URL scheme {parsed.scheme!r} not allowed (https required)"
    host = parsed.host
    if not host:
        return "callback URL has no host"
    try:
        infos = await asyncio.get_event_loop().run_in_executor(
            None, socket.getaddrinfo, host, parsed.port or (443 if parsed.scheme == "https" else 80)
        )
    except socket.gaierror:
        return f"callback host {host!r} does not resolve"
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return f"callback host resolves to unparseable address {addr!r}"
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return f"callback host resolves to disallowed address {addr}"
    return None


def sign_body(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


class CallbackDispatcher:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(15.0))

    async def deliver(self, task: Task, spec: PokeCallback | WebhookCallback) -> None:
        """Deliver with retries; records attempts/last_error on task.callback."""
        if task.callback is None:
            return
        for attempt, backoff in enumerate((*CALLBACK_BACKOFF_SECONDS, None), start=1):
            try:
                if isinstance(spec, PokeCallback):
                    await self._deliver_poke(task)
                else:
                    await self._deliver_webhook(task, spec)
                task.callback.attempts = attempt
                task.callback.delivered = True
                task.callback.last_error = None
                return
            except Exception as exc:  # noqa: BLE001 - record and retry
                task.callback.attempts = attempt
                task.callback.delivered = False
                task.callback.last_error = f"{type(exc).__name__}: {exc}"[:500]
                logger.warning("callback attempt %d for %s failed: %s", attempt, task.id, exc)
                if backoff is not None:
                    await asyncio.sleep(backoff)

    async def _deliver_poke(self, task: Task) -> None:
        if not self.settings.poke_api_key:
            raise RuntimeError("POKE_API_KEY is not configured")
        status_line = f"Hermes finished task {task.id} ({task.status})"
        detail = task.output if task.status == "completed" else task.error or ""
        limit = self.settings.poke_max_message_chars
        if detail and len(detail) > limit:
            detail = detail[:limit] + "\n\n[... truncated by poke-hermes-bridge]"
        message = f"{status_line}\n\n{detail}".rstrip()
        body: dict[str, Any] = {
            "message": message,
            "source": "poke-hermes-bridge",
            "task_id": task.id,
            "status": task.status,
        }
        if task.conversation_id:
            body["conversation_id"] = task.conversation_id
        if task.metadata:
            body["metadata"] = task.metadata
        resp = await self._http.post(
            self.settings.poke_inbound_url,
            json=body,
            headers={"Authorization": f"Bearer {self.settings.poke_api_key}"},
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"poke inbound API returned HTTP {resp.status_code}")

    async def _deliver_webhook(self, task: Task, spec: WebhookCallback) -> None:
        url = str(spec.url)
        reason = await check_callback_url(url, self.settings.bridge_allow_insecure_callbacks)
        if reason:
            raise RuntimeError(reason)
        body = task.model_dump_json().encode()
        timestamp = str(int(time.time()))
        secret = spec.secret or self.settings.bridge_webhook_secret
        if not secret:
            raise RuntimeError("no webhook secret configured for signing")
        event = {
            "completed": "task.completed",
            "failed": "task.failed",
            "cancelled": "task.cancelled",
        }.get(task.status, "task.status")
        resp = await self._http.post(
            url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Bridge-Timestamp": timestamp,
                "X-Bridge-Signature": sign_body(secret, timestamp, body),
                "X-Bridge-Event": event,
            },
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"webhook endpoint returned HTTP {resp.status_code}")


def verify_inbound_signature(
    secret: str, timestamp: str, raw_body: bytes, signature: str, max_skew: float = 300.0
) -> bool:
    """Verify an inbound X-Bridge-Signature + X-Bridge-Timestamp pair."""
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > max_skew:
        return False
    expected = sign_body(secret, timestamp, raw_body)
    return hmac.compare_digest(expected, signature)


class ReplayCache:
    """Small LRU guarding against webhook replay within the skew window."""

    def __init__(self, capacity: int = 512) -> None:
        self.capacity = capacity
        self._seen: dict[str, float] = {}

    def check_and_add(self, key: str) -> bool:
        """True if fresh (and recorded); False if replayed."""
        now = time.time()
        self._seen = {k: v for k, v in self._seen.items() if now - v < 600}
        if key in self._seen:
            return False
        if len(self._seen) >= self.capacity:
            self._seen.pop(next(iter(self._seen)))
        self._seen[key] = now
        return True


def body_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))
