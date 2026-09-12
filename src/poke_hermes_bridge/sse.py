"""Minimal, robust SSE parsing and framing helpers."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field


@dataclass
class SSEEvent:
    data: str = ""
    event: str | None = None
    id: str | None = None
    _data_lines: list[str] = field(default_factory=list, repr=False)


def parse_sse_lines(lines: AsyncIterator[str]) -> AsyncIterator[SSEEvent]:
    """Parse an async iterator of raw lines into SSE events.

    Handles ``data:`` (multi-line), ``event:``, ``id:``, comment lines
    (``: keepalive``) and blank-line dispatch. Returns an async iterator.
    """

    async def _gen() -> AsyncIterator[SSEEvent]:
        ev = SSEEvent()
        async for raw in lines:
            line = raw.rstrip("\r\n")
            if line == "":
                if ev._data_lines:
                    ev.data = "\n".join(ev._data_lines)
                    yield ev
                ev = SSEEvent()
                continue
            if line.startswith(":"):
                continue  # comment / keepalive
            if ":" in line:
                name, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
            else:
                name, value = line, ""
            if name == "data":
                ev._data_lines.append(value)
            elif name == "event":
                ev.event = value
            elif name == "id":
                ev.id = value
        if ev._data_lines:  # flush trailing unterminated event
            ev.data = "\n".join(ev._data_lines)
            yield ev

    return _gen()


def format_sse(data: str, event: str | None = None, event_id: str | int | None = None) -> str:
    """Serialize one SSE frame."""
    parts: list[str] = []
    if event is not None:
        parts.append(f"event: {event}")
    if event_id is not None:
        parts.append(f"id: {event_id}")
    for line in data.split("\n") or [""]:
        parts.append(f"data: {line}")
    return "\n".join(parts) + "\n\n"
