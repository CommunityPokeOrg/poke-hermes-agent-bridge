#!/usr/bin/env python3
"""Drive the bridge REST API: sync ask, then async task with SSE follow.

Usage: BRIDGE_URL=http://127.0.0.1:8700 BRIDGE_KEY=<key> python poke_client.py
"""

import asyncio
import os

import httpx

BASE = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8700")
KEY = os.environ["BRIDGE_KEY"]
HEADERS = {"Authorization": f"Bearer {KEY}"}


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, headers=HEADERS, timeout=60) as c:
        # sync ask
        r = await c.post("/v1/tasks", json={"prompt": "say hi in one word"})
        r.raise_for_status()
        print("sync output:", r.json()["output"])

        # async task
        r = await c.post("/v1/tasks", json={"prompt": "[tools] build a thing", "mode": "async"})
        task = r.json()
        print("task:", task["id"], task["status"])

        # follow events over SSE until a terminal bridge event
        event = None
        async with c.stream("GET", f"/v1/tasks/{task['id']}/events") as resp:
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and event in (
                    "task.completed",
                    "task.failed",
                ):
                    print("terminal:", event, line[5:].strip())
                    return


if __name__ == "__main__":
    asyncio.run(main())
