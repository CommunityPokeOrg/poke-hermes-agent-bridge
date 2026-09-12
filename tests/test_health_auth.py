import httpx
from httpx import ASGITransport

from conftest import AUTH, MOCK_KEY, make_settings
from poke_hermes_bridge.app import create_app
from poke_hermes_bridge.hermes.client import HermesClient


async def test_health(client: httpx.AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "poke-hermes-bridge"
    assert r.headers["x-request-id"]


async def test_ready_up(client: httpx.AsyncClient) -> None:
    r = await client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["hermes"]["reachable"] is True


async def test_ready_down() -> None:
    settings = make_settings()
    hermes = HermesClient(
        base_url="http://127.0.0.1:1",
        api_key=MOCK_KEY,
        timeout=1.0,
    )
    app = create_app(settings, hermes=hermes)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://bridge"
    ) as client:
        r = await client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["hermes"]["reachable"] is False


async def test_auth_required(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/capabilities")
    assert r.status_code == 401
    r = await client.post("/v1/tasks", json={"prompt": "hi"})
    assert r.status_code == 401
    r = await client.get("/v1/capabilities", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "unauthorized"
    assert "request_id" in body["error"]


async def test_per_key_task_scoping(client: httpx.AsyncClient) -> None:
    from conftest import OTHER_AUTH

    r = await client.post("/v1/tasks", json={"prompt": "hello"}, headers=AUTH)
    task_id = r.json()["id"]
    assert (await client.get(f"/v1/tasks/{task_id}", headers=AUTH)).status_code == 200
    r2 = await client.get(f"/v1/tasks/{task_id}", headers=OTHER_AUTH)
    assert r2.status_code == 404
