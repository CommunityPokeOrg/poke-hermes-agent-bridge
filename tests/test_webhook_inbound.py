import hashlib
import hmac
import json
import time

import httpx
import pytest
from httpx import ASGITransport

from conftest import AUTH, MOCK_KEY, make_settings
from poke_hermes_bridge.app import create_app
from poke_hermes_bridge.devtools.mock_hermes import create_mock_app
from poke_hermes_bridge.hermes.client import HermesClient

SECRET = "wh-secret-0123456789"


def sign(ts: str, body: bytes) -> str:
    mac = hmac.new(SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


@pytest.fixture
async def client() -> object:
    settings = make_settings(BRIDGE_WEBHOOK_SECRET=SECRET, POKE_API_KEY="pk")
    hermes = HermesClient(
        base_url="http://mock-hermes",
        api_key=MOCK_KEY,
        transport=ASGITransport(app=create_mock_app(api_key=MOCK_KEY)),
    )
    app = create_app(settings, hermes=hermes)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://bridge") as c:
        yield c


async def test_valid_webhook(client: httpx.AsyncClient) -> None:
    body = json.dumps({"prompt": "do stuff", "mode": "sync"}).encode()
    ts = str(int(time.time()))
    r = await client.post(
        "/v1/webhooks/poke",
        content=body,
        headers={"X-Bridge-Timestamp": ts, "X-Bridge-Signature": sign(ts, body)},
    )
    assert r.status_code == 202
    task = r.json()
    assert task["mode"] == "async"  # forced
    assert task["callback"]["type"] == "poke"  # defaulted (POKE_API_KEY set)


async def test_invalid_signature(client: httpx.AsyncClient) -> None:
    body = json.dumps({"prompt": "x"}).encode()
    ts = str(int(time.time()))
    r = await client.post(
        "/v1/webhooks/poke",
        content=body,
        headers={"X-Bridge-Timestamp": ts, "X-Bridge-Signature": "sha256=bad"},
    )
    assert r.status_code == 401


async def test_stale_timestamp(client: httpx.AsyncClient) -> None:
    body = json.dumps({"prompt": "x"}).encode()
    ts = str(int(time.time()) - 400)
    r = await client.post(
        "/v1/webhooks/poke",
        content=body,
        headers={"X-Bridge-Timestamp": ts, "X-Bridge-Signature": sign(ts, body)},
    )
    assert r.status_code == 401


async def test_replay_rejected(client: httpx.AsyncClient) -> None:
    body = json.dumps({"prompt": "x"}).encode()
    ts = str(int(time.time()))
    headers = {"X-Bridge-Timestamp": ts, "X-Bridge-Signature": sign(ts, body)}
    r1 = await client.post("/v1/webhooks/poke", content=body, headers=headers)
    r2 = await client.post("/v1/webhooks/poke", content=body, headers=headers)
    assert r1.status_code == 202
    assert r2.status_code == 409


async def test_webhook_disabled_404() -> None:
    settings = make_settings()  # no BRIDGE_WEBHOOK_SECRET
    hermes = HermesClient(
        base_url="http://mock-hermes",
        api_key=MOCK_KEY,
        transport=ASGITransport(app=create_mock_app(api_key=MOCK_KEY)),
    )
    app = create_app(settings, hermes=hermes)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://bridge"
    ) as client:
        r = await client.post("/v1/webhooks/poke", content=b"{}", headers=AUTH)
    assert r.status_code == 404
