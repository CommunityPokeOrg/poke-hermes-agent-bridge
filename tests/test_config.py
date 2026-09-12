from conftest import make_settings


def test_short_key_rejected() -> None:
    s = make_settings(BRIDGE_API_KEYS="short")
    problems = s.validate_startup()
    assert any("16" in p for p in problems)


def test_missing_hermes_key_rejected() -> None:
    s = make_settings(HERMES_API_KEY="")
    assert any("HERMES_API_KEY" in p for p in s.validate_startup())


def test_named_and_unnamed_keys() -> None:
    s = make_settings(BRIDGE_API_KEYS="alice:alicekey-0123456789ab,bobkey-0123456789ab")
    keys = s.api_keys()
    assert keys["alicekey-0123456789ab"] == "alice"
    assert keys["bobkey-0123456789ab"] == "default"


def test_valid_settings() -> None:
    assert make_settings().validate_startup() == []
