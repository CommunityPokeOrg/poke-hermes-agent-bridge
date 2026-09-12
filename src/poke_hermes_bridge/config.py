"""Runtime settings via environment variables (explicit names, no prefix)."""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Bridge configuration. ``BRIDGE_API_KEYS`` accepts comma-separated entries of
    either ``key`` or ``name:key``; the name identifies the caller in logs and in
    the Hermes session-key scope (``poke:<name>:<conversation_id>``)."""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    bridge_host: str = "0.0.0.0"  # noqa: S104 - bind address is operator-controlled
    bridge_port: int = 8700
    bridge_api_keys_raw: str = Field(
        default="",
        validation_alias="BRIDGE_API_KEYS",
        description="Comma-separated API keys, optionally 'name:key'.",
    )
    bridge_webhook_secret: str | None = Field(
        default=None, validation_alias="BRIDGE_WEBHOOK_SECRET"
    )
    bridge_public_url: str | None = Field(default=None, validation_alias="BRIDGE_PUBLIC_URL")

    bridge_sync_timeout_seconds: float = 120.0
    bridge_max_timeout_seconds: float = 600.0
    bridge_max_prompt_chars: int = 32000
    bridge_max_concurrent_tasks: int = 8
    bridge_rate_limit_per_minute: int = 60
    bridge_task_ttl_seconds: float = 3600.0
    bridge_max_events_per_task: int = 500
    bridge_poll_interval_seconds: float = 5.0
    bridge_allow_insecure_callbacks: bool = False
    bridge_log_level: str = "INFO"
    bridge_log_prompts: bool = False
    bridge_cors_origins: str | None = None

    hermes_base_url: str = "http://127.0.0.1:8642"
    hermes_api_key: str = Field(default="", validation_alias="HERMES_API_KEY")
    hermes_timeout_seconds: float = 120.0
    hermes_verify_tls: bool = True
    hermes_model: str = "hermes-agent"

    poke_api_key: str | None = Field(default=None, validation_alias="POKE_API_KEY")
    poke_inbound_url: str = "https://poke.com/api/v1/inbound/api-message"
    poke_max_message_chars: int = 8000

    @field_validator(
        "bridge_webhook_secret",
        "bridge_public_url",
        "poke_api_key",
        "bridge_cors_origins",
        mode="before",
    )
    @classmethod
    def _empty_is_none(cls, v):  # type: ignore[no-untyped-def]
        # an empty value in .env files must not enable features
        return None if v == "" else v

    @field_validator("hermes_api_key", mode="before")
    @classmethod
    def _empty_key(cls, v):  # type: ignore[no-untyped-def]
        return "" if v is None else v

    def api_keys(self) -> dict[str, str]:
        """Return {key: caller_name}. Nameless keys get the caller name 'default'."""
        out: dict[str, str] = {}
        for raw in self.bridge_api_keys_raw.split(","):
            entry = raw.strip()
            if not entry:
                continue
            if ":" in entry:
                name, key = entry.split(":", 1)
                out[key.strip()] = name.strip() or "default"
            else:
                out[entry] = "default"
        return out

    def validate_startup(self) -> list[str]:
        """Return a list of fatal configuration problems (empty == ok)."""
        problems: list[str] = []
        keys = self.api_keys()
        if not keys:
            problems.append("BRIDGE_API_KEYS must define at least one API key")
        for key in keys:
            if len(key) < 16:
                problems.append("BRIDGE_API_KEYS contains a key shorter than 16 characters")
        if not self.hermes_api_key:
            problems.append("HERMES_API_KEY is required")
        return problems

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in (self.bridge_cors_origins or "").split(",") if o.strip()]
