import httpx
from httpx import ASGITransport

from conftest import AUTH, MOCK_KEY, make_settings
from poke_hermes_bridge.app import create_app
from poke_hermes_bridge.devtools.mock_hermes import create_mock_app
from poke_hermes_bridge.hermes.client import HermesClient


async def test_sync_happy(client: httpx.AsyncClient, mock_app) -> None:  # type: ignore[no-untyped-def]
    r = await client.post(
        "/v1/tasks",
        json={"prompt": "summarise X", "conversation_id": "conv-1"},
        headers=AUTH,
    )
    assert r.status_code == 200
    task = r.json()
    assert task["status"] == "completed"
    assert task["output"] == "Hermes(mock) received: summarise X"
    assert task["usage"]["total_tokens"] == 15
    # caller-scoped session key was forwarded to Hermes
    assert "poke:caller:conv-1" in mock_app.state.mock.received_session_keys


async def test_sync_timeout() -> None:
    settings = make_settings()
    hermes = HermesClient(
        base_url="http://mock-hermes",
        api_key=MOCK_KEY,
        timeout=0.3,
        transport=ASGITransport(app=create_mock_app(api_key=MOCK_KEY)),
    )
    app = create_app(settings, hermes=hermes)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://bridge"
    ) as client:
        r = await client.post(
            "/v1/tasks",
            json={"prompt": "[slow] do it", "timeout_seconds": 1},
            headers=AUTH,
        )
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "hermes_timeout"


async def test_sync_hermes_auth_failure_no_leak() -> None:
    settings = make_settings()
    # wrong key -> mock returns 401 -> HermesAuthError -> 502
    hermes = HermesClient(
        base_url="http://mock-hermes",
        api_key="wrong-key-9999",
        transport=ASGITransport(app=create_mock_app(api_key=MOCK_KEY)),
    )
    app = create_app(settings, hermes=hermes)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://bridge"
    ) as client:
        r = await client.post("/v1/tasks", json={"prompt": "hi"}, headers=AUTH)
    assert r.status_code == 502
    body = r.json()
    assert body["error"]["code"] == "hermes_auth_failed"
    assert "wrong-key" not in r.text and MOCK_KEY not in r.text


async def test_metadata_echo(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/v1/tasks",
        json={"prompt": "hi", "metadata": {"k": "v"}},
        headers=AUTH,
    )
    assert r.json()["metadata"] == {"k": "v"}
