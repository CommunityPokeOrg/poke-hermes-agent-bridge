import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from poke_hermes_bridge.app import create_app
from poke_hermes_bridge.config import Settings
from poke_hermes_bridge.devtools.mock_hermes import create_mock_app
from poke_hermes_bridge.hermes.client import HermesClient

MOCK_KEY = "mock-key-0123456789ab"
BRIDGE_KEY = "test-key-0123456789abcdef"
OTHER_KEY = "other-key-0123456789abcdef"

AUTH = {"Authorization": f"Bearer {BRIDGE_KEY}"}
OTHER_AUTH = {"Authorization": f"Bearer {OTHER_KEY}"}


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "BRIDGE_API_KEYS": f"caller:{BRIDGE_KEY},other:{OTHER_KEY}",
        "HERMES_API_KEY": MOCK_KEY,
        "BRIDGE_POLL_INTERVAL_SECONDS": 0.05,
        "BRIDGE_RATE_LIMIT_PER_MINUTE": 0,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def mock_app() -> Any:
    return create_mock_app(api_key=MOCK_KEY)


@pytest.fixture
def hermes(settings: Settings, mock_app: Any) -> HermesClient:
    return HermesClient(
        base_url="http://mock-hermes",
        api_key=MOCK_KEY,
        transport=httpx.ASGITransport(app=mock_app),
    )


@pytest.fixture
def app(settings: Settings, hermes: HermesClient) -> Any:
    return create_app(settings, hermes=hermes)


@contextlib.asynccontextmanager
async def lifespan_client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    """Client with the app lifespan running. Must be entered inside the test
    coroutine (pytest-asyncio runs fixture teardown in a different task, which
    breaks the MCP session manager's anyio task group)."""
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            yield client


@pytest.fixture
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bridge"
    ) as c:
        yield c
