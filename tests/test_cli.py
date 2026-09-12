import pytest
from httpx import ASGITransport

import poke_hermes_bridge.hermes.client as hc
from conftest import MOCK_KEY
from poke_hermes_bridge.cli import main
from poke_hermes_bridge.devtools.mock_hermes import create_mock_app


def test_gen_key(capsys: pytest.CaptureFixture) -> None:
    assert main(["gen-key"]) == 0
    out = capsys.readouterr().out.strip()
    assert len(out) > 30


def test_check_against_mock(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    class FakeClient(hc.HermesClient):
        def __init__(self, base_url: str, api_key: str, **kw: object) -> None:
            super().__init__(
                base_url="http://mock-hermes",
                api_key=api_key,
                transport=ASGITransport(app=create_mock_app(api_key=MOCK_KEY)),
            )

    monkeypatch.setattr(hc, "HermesClient", FakeClient)
    monkeypatch.setenv("BRIDGE_API_KEYS", "cli-key-0123456789abcdef")
    monkeypatch.setenv("HERMES_API_KEY", MOCK_KEY)
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "reachable" in out
    assert "run_submission: True" in out


def test_check_unreachable(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("BRIDGE_API_KEYS", "cli-key-0123456789abcdef")
    monkeypatch.setenv("HERMES_API_KEY", MOCK_KEY)
    monkeypatch.setenv("HERMES_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("HERMES_TIMEOUT_SECONDS", "1")
    assert main(["check"]) == 1
    assert "UNREACHABLE" in capsys.readouterr().out


def test_poke_link(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("BRIDGE_API_KEYS", "cli-key-0123456789abcdef")
    monkeypatch.setenv("HERMES_API_KEY", MOCK_KEY)
    monkeypatch.setenv("BRIDGE_PUBLIC_URL", "https://bridge.example.com")
    assert main(["poke-link"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("https://poke.com/integrations/new?name=Hermes&url=")
    assert "mcp" in out
    assert MOCK_KEY not in out
