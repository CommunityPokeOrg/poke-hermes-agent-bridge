"""Async client for the Hermes Agent API server surface.

The API key is only ever sent as a ``Authorization: Bearer`` header; it is never
included in exception text or logged.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..sse import parse_sse_lines


class HermesError(Exception):
    """Base error for Hermes client failures."""


class HermesAuthError(HermesError):
    """Hermes rejected the configured API key (401/403)."""


class HermesUnavailable(HermesError):
    """Connection error or 5xx from Hermes."""


class HermesTimeout(HermesError):
    """The Hermes request timed out."""


class HermesRejected(HermesError):
    """A 4xx rejection carrying an OpenAI-style error body."""

    def __init__(self, status: int, code: str | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class HermesClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 120.0,
        verify_tls: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout),
            verify=verify_tls,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "HermesClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _error_message(resp: httpx.Response) -> tuple[str | None, str]:
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return None, resp.text[:500] or f"HTTP {resp.status_code}"
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            return (
                str(err.get("code")) if err.get("code") is not None else None,
                str(err.get("message") or "unknown error"),
            )
        if isinstance(payload, dict) and payload.get("detail"):
            return None, str(payload["detail"])
        return None, f"HTTP {resp.status_code}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise HermesTimeout(f"hermes request timed out: {method} {path}") from exc
        except httpx.HTTPError as exc:
            raise HermesUnavailable(f"hermes unreachable: {type(exc).__name__}") from exc
        if resp.status_code in (401, 403):
            raise HermesAuthError("hermes rejected the configured API key")
        if resp.status_code >= 500:
            raise HermesUnavailable(f"hermes returned HTTP {resp.status_code}")
        if resp.status_code >= 400:
            code, message = self._error_message(resp)
            raise HermesRejected(resp.status_code, code, message)
        return resp

    async def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        data = (await self._request(method, path, **kwargs)).json()
        if not isinstance(data, dict):
            raise HermesError(f"unexpected hermes response from {method} {path}")
        return data

    # -- health / capabilities ------------------------------------------

    async def health(self) -> dict[str, Any]:
        """GET /health (unauthenticated on the Hermes side)."""
        try:
            resp = await self._client.get("/health", headers={"Authorization": ""})
        except httpx.TimeoutException as exc:
            raise HermesTimeout("hermes health check timed out") from exc
        except httpx.HTTPError as exc:
            raise HermesUnavailable(f"hermes unreachable: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise HermesUnavailable(f"hermes /health returned HTTP {resp.status_code}")
        result: dict[str, Any] = resp.json()
        return result

    async def health_detailed(self) -> dict[str, Any]:
        return await self._json("GET", "/health/detailed")

    async def capabilities(self) -> dict[str, Any]:
        return await self._json("GET", "/v1/capabilities")

    # -- chat ------------------------------------------------------------

    @staticmethod
    def _session_headers(session_key: str | None, session_id: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if session_key:
            headers["X-Hermes-Session-Key"] = session_key
        if session_id:
            headers["X-Hermes-Session-Id"] = session_id
        return headers

    async def chat(
        self,
        messages: list[dict[str, Any]],
        session_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"messages": messages, "stream": False}
        if model:
            body["model"] = model
        kwargs: dict[str, Any] = {"json": body, "headers": self._session_headers(session_key)}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return await self._json("POST", "/v1/chat/completions", **kwargs)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        session_key: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream chat completions. Yields ``{"type": "delta", "text": ...}`` and
        ``{"type": "tool_progress", ...}`` events."""
        body: dict[str, Any] = {"messages": messages, "stream": True}
        if model:
            body["model"] = model
        try:
            async with self._client.stream(
                "POST",
                "/v1/chat/completions",
                json=body,
                headers=self._session_headers(session_key),
            ) as resp:
                await self._check_stream_response(resp, "/v1/chat/completions")
                async for ev in parse_sse_lines(resp.aiter_lines()):
                    if ev.data.strip() == "[DONE]":
                        return
                    if ev.event == "hermes.tool.progress":
                        yield {"type": "tool_progress", "raw": ev.data}
                        continue
                    try:
                        chunk = json.loads(ev.data)
                    except json.JSONDecodeError:
                        continue
                    for choice in chunk.get("choices") or []:
                        delta = (choice.get("delta") or {}).get("content")
                        if delta:
                            yield {"type": "delta", "text": delta}
        except httpx.TimeoutException as exc:
            raise HermesTimeout("hermes chat stream timed out") from exc
        except httpx.HTTPError as exc:
            raise HermesUnavailable(f"hermes unreachable: {type(exc).__name__}") from exc

    async def _check_stream_response(self, resp: httpx.Response, path: str) -> None:
        if resp.status_code in (401, 403):
            raise HermesAuthError("hermes rejected the configured API key")
        if resp.status_code >= 500:
            raise HermesUnavailable(f"hermes returned HTTP {resp.status_code}")
        if resp.status_code >= 400:
            await resp.aread()
            code, message = self._error_message(resp)
            raise HermesRejected(resp.status_code, code, message)

    # -- runs --------------------------------------------------------------

    async def create_run(
        self,
        input: str | list[dict[str, Any]],
        instructions: str | None = None,
        session_key: str | None = None,
        idempotency_key: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"input": input}
        if instructions:
            body["instructions"] = instructions
        if model:
            body["model"] = model
        headers = self._session_headers(session_key)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return await self._json("POST", "/v1/runs", json=body, headers=headers)

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/v1/runs/{run_id}")

    async def stream_run_events(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed ``data:`` JSON payloads from the run events SSE stream."""
        try:
            async with self._client.stream("GET", f"/v1/runs/{run_id}/events") as resp:
                await self._check_stream_response(resp, f"/v1/runs/{run_id}/events")
                async for ev in parse_sse_lines(resp.aiter_lines()):
                    if ev.data.strip() == "[DONE]":
                        return
                    try:
                        payload = json.loads(ev.data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        yield payload
        except httpx.TimeoutException as exc:
            raise HermesTimeout("hermes run events stream timed out") from exc
        except httpx.HTTPError as exc:
            raise HermesUnavailable(f"hermes unreachable: {type(exc).__name__}") from exc

    async def stop_run(self, run_id: str) -> dict[str, Any]:
        return await self._json("POST", f"/v1/runs/{run_id}/stop")

    async def steer_run(self, run_id: str, input: str) -> dict[str, Any]:
        return await self._json("POST", f"/v1/runs/{run_id}/steer", json={"input": input})

    async def resolve_approval(
        self, run_id: str, choice: str, request_id: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"choice": choice}
        if request_id:
            body["request_id"] = request_id
        return await self._json("POST", f"/v1/runs/{run_id}/approval", json=body)
