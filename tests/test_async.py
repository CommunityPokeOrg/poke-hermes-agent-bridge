import asyncio
import json

import httpx
from httpx import ASGITransport

from conftest import AUTH, MOCK_KEY, make_settings
from poke_hermes_bridge.app import create_app
from poke_hermes_bridge.devtools.mock_hermes import create_mock_app
from poke_hermes_bridge.hermes.client import HermesClient


async def wait_task(client: httpx.AsyncClient, task_id: str, timeout: float = 5.0) -> dict:
    for _ in range(int(timeout / 0.05)):
        r = await client.get(f"/v1/tasks/{task_id}", headers=AUTH)
        task = r.json()
        if task["status"] in ("completed", "failed", "cancelled"):
            return task
        await asyncio.sleep(0.05)
    raise AssertionError(f"task {task_id} did not finish")


async def wait_running(client: httpx.AsyncClient, task_id: str, timeout: float = 5.0) -> dict:
    """Wait until an async task has a live Hermes run (status running)."""
    for _ in range(int(timeout / 0.05)):
        task = (await client.get(f"/v1/tasks/{task_id}", headers=AUTH)).json()
        if task["hermes_run_id"] or task["status"] in ("completed", "failed", "cancelled"):
            return task
        await asyncio.sleep(0.05)
    raise AssertionError(f"task {task_id} never got a hermes run")


async def test_async_lifecycle_and_events(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/v1/tasks",
        json={"prompt": "[tools] build me a thing", "mode": "async"},
        headers=AUTH,
    )
    assert r.status_code == 202
    task = r.json()
    assert task["status"] == "queued"

    async with client.stream("GET", f"/v1/tasks/{task['id']}/events", headers=AUTH) as resp:
        assert resp.status_code == 200
        seen: set[str] = set()
        last_ids: list[str] = []
        event_name = None
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                event_name = line[7:]
                seen.add(event_name)
            elif line.startswith("id: "):
                last_ids.append(line[4:])
            elif line.startswith("data: ") and event_name == "task.completed":
                payload = json.loads(line[6:])
                assert payload["output"].startswith("Hermes(mock) received:")
                break
    assert "task.created" in seen
    assert "hermes.tool.started" in seen
    assert "task.completed" in seen
    assert last_ids  # sequence ids present

    task = await wait_task(client, task["id"])
    assert task["status"] == "completed"
    assert "Hermes(mock) received:" in task["output"]
    assert task["hermes_run_id"]


async def test_async_replay_with_last_event_id(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/tasks", json={"prompt": "hi", "mode": "async"}, headers=AUTH)
    task = await wait_task(client, r.json()["id"])
    # replayed stream for a terminal task ends right after buffered events
    r = await client.get(f"/v1/tasks/{task['id']}/events", headers=AUTH)
    assert "task.completed" in r.text
    r2 = await client.get(f"/v1/tasks/{task['id']}/events", headers={**AUTH, "Last-Event-ID": "1"})
    assert "task.created" not in r2.text


async def test_async_fallback_no_run_submission() -> None:
    settings = make_settings()
    hermes = HermesClient(
        base_url="http://mock-hermes",
        api_key=MOCK_KEY,
        transport=ASGITransport(app=create_mock_app(api_key=MOCK_KEY, run_submission=False)),
    )
    app = create_app(settings, hermes=hermes)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://bridge"
    ) as client:
        r = await client.post(
            "/v1/tasks", json={"prompt": "hi there", "mode": "async"}, headers=AUTH
        )
        assert r.status_code == 202
        task = await wait_task(client, r.json()["id"])
        assert task["status"] == "completed"
        assert task["hermes_run_id"] is None
        assert task["output"] == "Hermes(mock) received: hi there"


async def test_stop(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/v1/tasks", json={"prompt": "[slow] work", "mode": "async"}, headers=AUTH
    )
    task_id = r.json()["id"]
    await wait_running(client, task_id)
    r = await client.post(f"/v1/tasks/{task_id}/stop", headers=AUTH)
    assert r.status_code == 200
    task = await wait_task(client, task_id)
    assert task["status"] == "cancelled"


async def test_steer_accepted_then_409_on_terminal(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/v1/tasks", json={"prompt": "[slow] work", "mode": "async"}, headers=AUTH
    )
    task_id = r.json()["id"]
    await wait_running(client, task_id)
    r = await client.post(f"/v1/tasks/{task_id}/steer", json={"input": "go faster"}, headers=AUTH)
    assert r.status_code == 200
    await wait_task(client, task_id)
    r = await client.post(f"/v1/tasks/{task_id}/steer", json={"input": "again"}, headers=AUTH)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "task_not_active"


async def test_stop_terminal_409(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/tasks", json={"prompt": "hi", "mode": "async"}, headers=AUTH)
    task_id = r.json()["id"]
    await wait_task(client, task_id)
    r = await client.post(f"/v1/tasks/{task_id}/stop", headers=AUTH)
    assert r.status_code == 409


async def test_run_receives_string_input_and_instructions(
    client: httpx.AsyncClient,
    mock_app,  # type: ignore[no-untyped-def]
) -> None:
    r = await client.post(
        "/v1/tasks",
        json={"prompt": "do X", "instructions": "be terse", "mode": "async"},
        headers=AUTH,
    )
    assert r.status_code == 202
    for _ in range(100):
        if mock_app.state.mock.received_runs:
            break
        await asyncio.sleep(0.05)
    rec = mock_app.state.mock.received_runs[-1]
    assert rec["input"] == "do X"
    assert rec["instructions"] == "be terse"


async def test_failed_run(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/v1/tasks", json={"prompt": "[fail] boom", "mode": "async"}, headers=AUTH
    )
    task = await wait_task(client, r.json()["id"])
    assert task["status"] == "failed"
    assert task["error"]
