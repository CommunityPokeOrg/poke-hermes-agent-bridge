import asyncio

import httpx
from httpx import ASGITransport

from conftest import AUTH, MOCK_KEY, make_settings
from poke_hermes_bridge.app import create_app
from poke_hermes_bridge.devtools.mock_hermes import create_mock_app
from poke_hermes_bridge.hermes.client import HermesClient


async def test_idempotent_replay_and_conflict(client: httpx.AsyncClient) -> None:
    body = {"prompt": "hello", "mode": "sync"}
    h = {**AUTH, "Idempotency-Key": "k-1"}
    r1 = await client.post("/v1/tasks", json=body, headers=h)
    assert r1.status_code == 200
    r2 = await client.post("/v1/tasks", json=body, headers=h)
    assert r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]
    r3 = await client.post("/v1/tasks", json={"prompt": "different"}, headers=h)
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "idempotency_conflict"


async def test_rate_limit() -> None:
    settings = make_settings(BRIDGE_RATE_LIMIT_PER_MINUTE=3)
    hermes = HermesClient(
        base_url="http://mock-hermes",
        api_key=MOCK_KEY,
        transport=ASGITransport(app=create_mock_app(api_key=MOCK_KEY)),
    )
    app = create_app(settings, hermes=hermes)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://bridge"
    ) as client:
        codes = [(await client.get("/v1/capabilities", headers=AUTH)).status_code for _ in range(5)]
        r = await client.get("/v1/capabilities", headers=AUTH)
    assert 429 in codes
    assert r.status_code == 429
    assert "retry-after" in r.headers
    assert r.json()["error"]["code"] == "rate_limited"


async def test_concurrency_cap() -> None:
    settings = make_settings(BRIDGE_MAX_CONCURRENT_TASKS=1)
    hermes = HermesClient(
        base_url="http://mock-hermes",
        api_key=MOCK_KEY,
        transport=ASGITransport(app=create_mock_app(api_key=MOCK_KEY)),
    )
    app = create_app(settings, hermes=hermes)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://bridge"
    ) as client:
        r1 = await client.post(
            "/v1/tasks", json={"prompt": "[slow] a", "mode": "async"}, headers=AUTH
        )
        assert r1.status_code == 202
        await asyncio.sleep(0.1)
        r2 = await client.post("/v1/tasks", json={"prompt": "b", "mode": "async"}, headers=AUTH)
        assert r2.status_code == 429
        assert r2.json()["error"]["code"] == "too_many_active_tasks"
