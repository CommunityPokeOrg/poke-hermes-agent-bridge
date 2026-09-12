from conftest import AUTH, lifespan_client

JSONRPC_HEADERS = {
    **AUTH,
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def rpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}


async def test_mcp_requires_auth(app) -> None:  # type: ignore[no-untyped-def]
    async with lifespan_client(app) as client:
        r = await client.post("/mcp/", json=rpc("initialize"))
    assert r.status_code == 401


async def test_mcp_tools_list_and_call(app) -> None:  # type: ignore[no-untyped-def]
    async with lifespan_client(app) as client:
        init = rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        )
        r = await client.post("/mcp/", json=init, headers=JSONRPC_HEADERS)
        assert r.status_code == 200, r.text

        r = await client.post("/mcp/", json=rpc("tools/list", req_id=2), headers=JSONRPC_HEADERS)
        assert r.status_code == 200
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert {
            "hermes_ask",
            "hermes_start_task",
            "hermes_task_status",
            "hermes_stop_task",
            "hermes_steer_task",
            "hermes_health",
        } <= names

        r = await client.post(
            "/mcp/",
            json=rpc("tools/call", {"name": "hermes_ask", "arguments": {"prompt": "ping"}}, 3),
            headers=JSONRPC_HEADERS,
        )
        assert r.status_code == 200
        result = r.json()["result"]
        text = result["content"][0]["text"]
        assert "Hermes(mock) received: ping" in text

        r = await client.post(
            "/mcp/",
            json=rpc("tools/call", {"name": "hermes_health", "arguments": {}}, 4),
            headers=JSONRPC_HEADERS,
        )
        assert r.json()["result"]["content"][0]["text"]
