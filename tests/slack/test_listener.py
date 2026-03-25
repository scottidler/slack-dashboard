from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from slack_dashboard.slack.listener import SocketListener
from slack_dashboard.slack.queue import PRIORITY_SOCKET_EVENT, FetchQueue
from slack_dashboard.thread import ThreadEntry


def _make_request(
    envelope_id: str = "env-1",
    req_type: str = "events_api",
    event_type: str = "message",
    channel: str = "C111",
    thread_ts: str | None = "1234.5678",
    user: str = "U999",
    ts: str = "1711300000.000000",
) -> AsyncMock:
    event: dict[str, str] = {"type": event_type, "channel": channel, "user": user, "ts": ts}
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    req = AsyncMock()
    req.envelope_id = envelope_id
    req.type = req_type
    req.payload = {"event": event}
    return req


def _make_thread(
    channel_id: str = "C111",
    thread_ts: str = "1234.5678",
    reply_count: int = 5,
) -> ThreadEntry:
    return ThreadEntry(
        channel_id=channel_id,
        channel_name="general",
        thread_ts=thread_ts,
        first_message="test",
        started_by="U1",
        reply_count=reply_count,
        participants={"U1": 2, "U2": 1},
        last_activity=datetime(2026, 3, 24, 12, 0, 0, tzinfo=UTC),
    )


def _make_listener(
    threads: dict[tuple[str, str], ThreadEntry] | None = None,
) -> tuple[SocketListener, FetchQueue]:
    queue = FetchQueue()
    if threads is None:
        threads = {}
    listener = SocketListener(
        queue=queue,
        threads=threads,
        channel_ids={"C111", "C222"},
        channel_names={"C111": "general", "C222": "random"},
    )
    return listener, queue


@pytest.mark.asyncio
async def test_acknowledges_event() -> None:
    listener, _ = _make_listener()
    client = AsyncMock()
    req = _make_request()
    await listener.handle_event(client, req)
    client.send_socket_mode_response.assert_called_once()
    resp = client.send_socket_mode_response.call_args[0][0]
    assert resp.envelope_id == "env-1"


@pytest.mark.asyncio
async def test_ignores_non_events_api() -> None:
    listener, queue = _make_listener()
    client = AsyncMock()
    req = _make_request(req_type="hello")
    await listener.handle_event(client, req)
    assert queue.pending_count == 0


@pytest.mark.asyncio
async def test_ignores_non_message_event() -> None:
    listener, queue = _make_listener()
    client = AsyncMock()
    req = _make_request(event_type="reaction_added")
    await listener.handle_event(client, req)
    assert queue.pending_count == 0


@pytest.mark.asyncio
async def test_ignores_unconfigured_channel() -> None:
    listener, queue = _make_listener()
    client = AsyncMock()
    req = _make_request(channel="C999")
    await listener.handle_event(client, req)
    assert queue.pending_count == 0


@pytest.mark.asyncio
async def test_ignores_standalone_message() -> None:
    listener, queue = _make_listener()
    client = AsyncMock()
    req = _make_request(thread_ts=None)
    await listener.handle_event(client, req)
    assert queue.pending_count == 0


@pytest.mark.asyncio
async def test_queues_thread_reply() -> None:
    listener, queue = _make_listener()
    client = AsyncMock()
    req = _make_request()
    await listener.handle_event(client, req)
    assert queue.pending_count == 1
    item = await queue.dequeue()
    assert item.priority == PRIORITY_SOCKET_EVENT
    assert item.channel_id == "C111"
    assert item.channel_name == "general"
    assert item.thread_ts == "1234.5678"


@pytest.mark.asyncio
async def test_updates_existing_thread_metadata() -> None:
    thread = _make_thread(reply_count=5)
    threads = {("C111", "1234.5678"): thread}
    listener, _ = _make_listener(threads=threads)
    client = AsyncMock()
    req = _make_request(user="U_NEW", ts="1774440000.000000")
    await listener.handle_event(client, req)

    assert thread.reply_count == 6
    assert "U_NEW" in thread.participants
    assert thread.last_activity == datetime.fromtimestamp(1774440000.0, tz=UTC)


@pytest.mark.asyncio
async def test_does_not_regress_last_activity() -> None:
    thread = _make_thread()
    thread.last_activity = datetime(2026, 3, 25, 12, 0, 0, tzinfo=UTC)
    threads = {("C111", "1234.5678"): thread}
    listener, _ = _make_listener(threads=threads)
    client = AsyncMock()
    req = _make_request(ts="1711300000.000000")
    await listener.handle_event(client, req)
    assert thread.last_activity == datetime(2026, 3, 25, 12, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_new_thread_not_in_store_still_queued() -> None:
    listener, queue = _make_listener(threads={})
    client = AsyncMock()
    req = _make_request(thread_ts="9999.9999")
    await listener.handle_event(client, req)
    assert queue.pending_count == 1
