from unittest.mock import AsyncMock

import pytest

from slack_dashboard.config import AppConfig
from slack_dashboard.slack.client import SlackClient
from slack_dashboard.slack.poller import SlackPoller
from slack_dashboard.slack.queue import PRIORITY_BACKFILL, PRIORITY_SOCKET_EVENT, FetchItem


def _make_mock_slack() -> AsyncMock:
    client = AsyncMock(spec=SlackClient)
    client.resolve_channels = AsyncMock(return_value={"general": "C111"})
    client.fetch_threads = AsyncMock(
        return_value=[
            {"ts": "1.1", "text": "Root message", "reply_count": 3, "thread_ts": "1.1"},
        ]
    )
    client.fetch_replies = AsyncMock(
        return_value=[
            {"ts": "1.1", "user": "U1", "text": "root"},
            {"ts": "1.2", "user": "U2", "text": "reply 1"},
            {"ts": "1.3", "user": "U3", "text": "reply 2"},
            {"ts": "1.4", "user": "U1", "text": "reply 3"},
        ]
    )
    return client


@pytest.mark.asyncio
async def test_fetch_channel_via_process_item() -> None:
    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config)
    item = FetchItem(priority=PRIORITY_BACKFILL, channel_id="C111", channel_name="general")
    await poller._process_item(item)
    assert len(poller.threads) == 1
    key = ("C111", "1.1")
    assert key in poller.threads
    entry = poller.threads[key]
    assert entry.reply_count == 3
    assert len(entry.participants) == 3
    assert entry.channel_name == "general"
    assert entry.first_message == "Root message"


@pytest.mark.asyncio
async def test_fetch_thread_via_process_item() -> None:
    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config)
    item = FetchItem(
        priority=PRIORITY_SOCKET_EVENT,
        channel_id="C111",
        channel_name="general",
        thread_ts="1.1",
    )
    await poller._process_item(item)
    assert len(poller.threads) == 1
    key = ("C111", "1.1")
    entry = poller.threads[key]
    assert entry.reply_count == 3
    assert entry.first_message == "root"


@pytest.mark.asyncio
async def test_ranked_threads() -> None:
    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config)
    item = FetchItem(priority=PRIORITY_BACKFILL, channel_id="C111", channel_name="general")
    await poller._process_item(item)
    ranked = poller.ranked_threads()
    assert len(ranked) == 1
    assert ranked[0].heat_score > 0


@pytest.mark.asyncio
async def test_preserves_existing_title() -> None:
    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config)

    item = FetchItem(priority=PRIORITY_BACKFILL, channel_id="C111", channel_name="general")
    await poller._process_item(item)
    key = ("C111", "1.1")
    poller.threads[key].title = "Existing Title"
    poller.threads[key].title_watermark = 3

    await poller._fetch_channel("C111", "general")
    assert poller.threads[key].title == "Existing Title"
    assert poller.threads[key].title_watermark == 3


@pytest.mark.asyncio
async def test_queue_seeded_on_start() -> None:
    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111", "random": "C222"})
    poller = SlackPoller(mock_slack, config)
    await poller.start()
    assert poller.queue.pending_count == 2
    await poller.stop()


@pytest.mark.asyncio
async def test_queue_property_accessible() -> None:
    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config)
    assert poller.queue.pending_count == 0
