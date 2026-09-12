import asyncio
import hashlib
import hmac
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport

from conftest import AUTH, MOCK_KEY, make_settings
from poke_hermes_bridge import callbacks
from poke_hermes_bridge.app import create_app
from poke_hermes_bridge.callbacks import CallbackDispatcher, check_callback_url
from poke_hermes_bridge.devtools.mock_hermes import create_mock_app
from poke_hermes_bridge.hermes.client import HermesClient
from poke_hermes_bridge.service import BridgeService


def make_hermes() -> HermesClient:
    return HermesClient(
        base_url="http://mock-hermes",
        api_key=MOCK_KEY,
        transport=ASGITransport(app=create_mock_app(api_key=MOCK_KEY)),
    )


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(callbacks, "CALLBACK_BACKOFF_SECONDS", (0.01, 0.01, 0.01))


def fake_poke_app(calls: list[dict]) -> FastAPI:
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.post("/api/v1/inbound/api-message")
    async def inbound(request: Request) -> object:
        calls.append(
            {
                "auth": request.headers.get("authorization"),
                "body": await request.json(),
            }
        )
        if len(calls) == 1:  # fail first delivery to exercise retries
            return JSONResponse({"error": "boom"}, status_code=500)
        return {"ok": True}

    return app


async def test_poke_callback_retries_and_delivers() -> None:
    calls: list[dict] = []
    settings = make_settings(POKE_API_KEY="poke-key-xyz")
    hermes = make_hermes()
    dispatcher = CallbackDispatcher(
        settings, http=httpx.AsyncClient(transport=ASGITransport(app=fake_poke_app(calls)))
    )
    service = BridgeService(settings, hermes, callbacks=dispatcher)
    app = create_app(settings, hermes=hermes, service=service)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://bridge"
    ) as client:
        r = await client.post(
            "/v1/tasks",
            json={
                "prompt": "hello",
                "mode": "async",
                "callback": {"type": "poke"},
                "metadata": {"origin": "test"},
            },
            headers=AUTH,
        )
        task_id = r.json()["id"]
        for _ in range(100):
            await asyncio.sleep(0.05)
            t = (await client.get(f"/v1/tasks/{task_id}", headers=AUTH)).json()
            if t["callback"]["delivered"] is not None:
                break
        assert t["status"] == "completed"
        assert t["callback"]["delivered"] is True
        assert t["callback"]["attempts"] == 2  # first attempt got a 500
    assert calls[0]["auth"] == "Bearer poke-key-xyz"
    msg = calls[-1]["body"]
    assert task_id in msg["message"]
    assert msg["metadata"] == {"origin": "test"}
    assert "Hermes(mock) received" in msg["message"]


async def test_webhook_callback_signature() -> None:
    calls: list[dict] = []
    hook = FastAPI()

    @hook.post("/cb")
    async def cb(request: Request) -> dict:
        calls.append(
            {
                "body": await request.body(),
                "ts": request.headers.get("x-bridge-timestamp"),
                "sig": request.headers.get("x-bridge-signature"),
                "event": request.headers.get("x-bridge-event"),
            }
        )
        return {"ok": True}

    settings = make_settings(BRIDGE_ALLOW_INSECURE_CALLBACKS=True)
    hermes = make_hermes()
    dispatcher = CallbackDispatcher(
        settings, http=httpx.AsyncClient(transport=ASGITransport(app=hook))
    )
    service = BridgeService(settings, hermes, callbacks=dispatcher)
    app = create_app(settings, hermes=hermes, service=service)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://bridge"
    ) as client:
        r = await client.post(
            "/v1/tasks",
            json={
                "prompt": "hi",
                "mode": "async",
                "callback": {
                    "type": "webhook",
                    "url": "http://callback.test/cb",
                    "secret": "hook-secret",
                },
            },
            headers=AUTH,
        )
        task_id = r.json()["id"]
        for _ in range(100):
            await asyncio.sleep(0.05)
            t = (await client.get(f"/v1/tasks/{task_id}", headers=AUTH)).json()
            if t["callback"]["delivered"] is not None:
                break
        assert t["callback"]["delivered"] is True
    assert calls
    call = calls[0]
    assert call["event"] == "task.completed"
    expected = hmac.new(
        b"hook-secret", call["ts"].encode() + b"." + call["body"], hashlib.sha256
    ).hexdigest()
    assert call["sig"] == f"sha256={expected}"
    assert json.loads(call["body"])["id"] == task_id


async def test_ssrf_guard_blocks_loopback() -> None:
    assert await check_callback_url("http://127.0.0.1/hook", allow_insecure=False)
    assert await check_callback_url("https://localhost/hook", allow_insecure=False)
    assert await check_callback_url("http://127.0.0.1/hook", allow_insecure=True) is None
    assert await check_callback_url("https://nonexistent.invalid/hook", allow_insecure=False)
