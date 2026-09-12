"""Pydantic models for the bridge REST protocol."""

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, HttpUrl, StringConstraints, field_validator

CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
TASK_STATUSES = (
    "queued",
    "running",
    "waiting_for_approval",
    "stopping",
    "completed",
    "failed",
    "cancelled",
)
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
TaskStatus = Literal[
    "queued", "running", "waiting_for_approval", "stopping", "completed", "failed", "cancelled"
]


class PokeCallback(BaseModel):
    type: Literal["poke"] = "poke"


class WebhookCallback(BaseModel):
    type: Literal["webhook"] = "webhook"
    url: HttpUrl
    secret: str | None = None


CallbackSpec = Annotated[PokeCallback | WebhookCallback, Field(discriminator="type")]

ConversationId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:\-]{1,128}$")]


class TaskCreate(BaseModel):
    prompt: str = Field(min_length=1)
    instructions: str | None = None
    conversation_id: ConversationId | None = None
    mode: Literal["sync", "async"] = "sync"
    timeout_seconds: float | None = None
    callback: CallbackSpec | None = None
    metadata: dict[str, str] | None = None

    @field_validator("metadata")
    @classmethod
    def _metadata_bounds(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return v
        if len(v) > 16:
            raise ValueError("metadata may have at most 16 keys")
        for key, value in v.items():
            if len(value) > 512:
                raise ValueError(f"metadata[{key!r}] exceeds 512 characters")
        return v


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class CallbackStatus(BaseModel):
    type: str
    delivered: bool | None = None
    attempts: int = 0
    last_error: str | None = None


class Task(BaseModel):
    id: str
    status: TaskStatus = "queued"
    mode: Literal["sync", "async"] = "async"
    conversation_id: str | None = None
    created_at: float
    updated_at: float
    hermes_run_id: str | None = None
    output: str | None = None
    error: str | None = None
    usage: Usage | None = None
    metadata: dict[str, str] | None = None
    callback: CallbackStatus | None = None


class SteerRequest(BaseModel):
    input: str = Field(min_length=1)


class ApprovalRequest(BaseModel):
    choice: Literal["once", "always", "deny"]
    request_id: str | None = None


def parse_hermes_error(payload: Any) -> str:
    """Extract a message from an OpenAI-style Hermes error body."""
    if not isinstance(payload, dict):
        return "unknown error"
    err = payload.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err)
    detail = payload.get("detail") or payload
    return str(detail)
