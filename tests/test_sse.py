from collections.abc import AsyncIterator

from poke_hermes_bridge.sse import format_sse, parse_sse_lines


async def feed(lines: list[str]) -> AsyncIterator[str]:
    for line in lines:
        yield line


async def collect(lines: list[str]) -> list:
    return [ev async for ev in parse_sse_lines(feed(lines))]


async def test_basic_event() -> None:
    events = await collect(["data: hello\n", "\n"])
    assert len(events) == 1
    assert events[0].data == "hello"
    assert events[0].event is None


async def test_event_and_id() -> None:
    events = await collect(
        ["id: 7\n", "event: task.completed\n", "data: {}\n", "\n", "data: [DONE]\n", "\n"]
    )
    assert events[0].id == "7"
    assert events[0].event == "task.completed"
    assert events[1].data == "[DONE]"


async def test_multiline_data_and_comments() -> None:
    events = await collect([": keepalive\n", "data: a\n", "data: b\n", "\n", ": stream closed\n"])
    assert events[0].data == "a\nb"
    assert len(events) == 1


async def test_no_space_after_colon() -> None:
    events = await collect(["data:x\n", "\n"])
    assert events[0].data == "x"


def test_format_sse() -> None:
    frame = format_sse("{}", event="task.created", event_id=3)
    assert frame == "event: task.created\nid: 3\ndata: {}\n\n"
