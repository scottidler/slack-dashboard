import asyncio
import contextlib

import pytest

from slack_dashboard.slack.queue import (
    PRIORITY_BACKFILL,
    PRIORITY_REFRESH,
    PRIORITY_SOCKET_EVENT,
    FetchItem,
    FetchQueue,
)


def test_fetch_item_ordering() -> None:
    high = FetchItem(priority=PRIORITY_SOCKET_EVENT, channel_id="C1", channel_name="general")
    mid = FetchItem(priority=PRIORITY_BACKFILL, channel_id="C2", channel_name="random")
    low = FetchItem(priority=PRIORITY_REFRESH, channel_id="C3", channel_name="ops")
    items = sorted([low, mid, high])
    assert items[0].priority == PRIORITY_SOCKET_EVENT
    assert items[1].priority == PRIORITY_BACKFILL
    assert items[2].priority == PRIORITY_REFRESH


def test_fetch_item_with_thread_ts() -> None:
    item = FetchItem(
        priority=PRIORITY_SOCKET_EVENT,
        channel_id="C1",
        channel_name="general",
        thread_ts="1234.5678",
    )
    assert item.thread_ts == "1234.5678"
    assert item.priority == PRIORITY_SOCKET_EVENT


def test_fetch_item_default_thread_ts() -> None:
    item = FetchItem(priority=PRIORITY_BACKFILL, channel_id="C1", channel_name="general")
    assert item.thread_ts is None


@pytest.mark.asyncio
async def test_enqueue_dequeue_priority_order() -> None:
    q = FetchQueue()
    q.enqueue(FetchItem(priority=PRIORITY_REFRESH, channel_id="C3", channel_name="ops"))
    q.enqueue(FetchItem(priority=PRIORITY_SOCKET_EVENT, channel_id="C1", channel_name="general"))
    q.enqueue(FetchItem(priority=PRIORITY_BACKFILL, channel_id="C2", channel_name="random"))

    first = await q.dequeue()
    assert first.priority == PRIORITY_SOCKET_EVENT
    assert first.channel_id == "C1"

    second = await q.dequeue()
    assert second.priority == PRIORITY_BACKFILL
    assert second.channel_id == "C2"

    third = await q.dequeue()
    assert third.priority == PRIORITY_REFRESH
    assert third.channel_id == "C3"


@pytest.mark.asyncio
async def test_dedup_same_channel() -> None:
    q = FetchQueue()
    assert q.enqueue(FetchItem(priority=PRIORITY_BACKFILL, channel_id="C1", channel_name="gen"))
    assert not q.enqueue(
        FetchItem(priority=PRIORITY_SOCKET_EVENT, channel_id="C1", channel_name="gen")
    )
    assert q.pending_count == 1


@pytest.mark.asyncio
async def test_dedup_different_thread_ts() -> None:
    q = FetchQueue()
    assert q.enqueue(
        FetchItem(
            priority=PRIORITY_BACKFILL,
            channel_id="C1",
            channel_name="gen",
            thread_ts="1.1",
        )
    )
    assert q.enqueue(
        FetchItem(
            priority=PRIORITY_BACKFILL,
            channel_id="C1",
            channel_name="gen",
            thread_ts="2.2",
        )
    )
    assert q.pending_count == 2


@pytest.mark.asyncio
async def test_dedup_clears_after_dequeue() -> None:
    q = FetchQueue()
    q.enqueue(FetchItem(priority=PRIORITY_BACKFILL, channel_id="C1", channel_name="gen"))
    assert not q.enqueue(FetchItem(priority=PRIORITY_BACKFILL, channel_id="C1", channel_name="gen"))
    await q.dequeue()
    assert q.enqueue(FetchItem(priority=PRIORITY_BACKFILL, channel_id="C1", channel_name="gen"))


@pytest.mark.asyncio
async def test_seed_channels() -> None:
    q = FetchQueue()
    channels = {"general": "C1", "random": "C2", "ops": "C3"}
    count = q.seed_channels(channels)
    assert count == 3
    assert q.pending_count == 3

    items = []
    for _ in range(3):
        items.append(await q.dequeue())
    assert all(item.priority == PRIORITY_BACKFILL for item in items)
    assert {item.channel_id for item in items} == {"C1", "C2", "C3"}


@pytest.mark.asyncio
async def test_seed_channels_dedup() -> None:
    q = FetchQueue()
    channels = {"general": "C1", "random": "C2"}
    q.seed_channels(channels)
    count = q.seed_channels(channels)
    assert count == 0


@pytest.mark.asyncio
async def test_dequeue_blocks_when_empty() -> None:
    q = FetchQueue()
    result: list[FetchItem] = []

    async def consumer() -> None:
        item = await q.dequeue()
        result.append(item)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.05)
    assert not result

    q.enqueue(FetchItem(priority=PRIORITY_BACKFILL, channel_id="C1", channel_name="gen"))
    await asyncio.sleep(0.05)
    assert len(result) == 1
    assert result[0].channel_id == "C1"
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
