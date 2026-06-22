import time
from unittest.mock import AsyncMock

import pytest

from slack_dashboard.config import AppConfig
from slack_dashboard.slack.client import SlackClient
from slack_dashboard.slack.poller import SlackPoller
from slack_dashboard.slack.queue import (
    PRIORITY_BACKFILL,
    PRIORITY_REFRESH,
    PRIORITY_SOCKET_EVENT,
    FetchItem,
)

_NOW = str(time.time())
_REPLY_1 = str(time.time() + 1)
_REPLY_2 = str(time.time() + 2)
_REPLY_3 = str(time.time() + 3)


def _make_mock_slack() -> AsyncMock:
    client = AsyncMock(spec=SlackClient)
    client.resolve_channels = AsyncMock(return_value={"general": "C111"})
    client.resolve_user = AsyncMock(side_effect=lambda uid: uid)
    client.fetch_threads = AsyncMock(
        return_value=[
            {"ts": _NOW, "text": "Root message", "reply_count": 3, "thread_ts": _NOW},
        ]
    )
    client.fetch_replies = AsyncMock(
        return_value=[
            {"ts": _NOW, "user": "U1", "text": "root"},
            {"ts": _REPLY_1, "user": "U2", "text": "reply 1"},
            {"ts": _REPLY_2, "user": "U3", "text": "reply 2"},
            {"ts": _REPLY_3, "user": "U1", "text": "reply 3"},
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
    key = ("C111", _NOW)
    assert key in poller.threads
    entry = poller.threads[key]
    assert entry.reply_count == 3
    assert len(entry.participants) == 3
    assert entry.channel_name == "general"
    assert entry.first_message == "root"


@pytest.mark.asyncio
async def test_fetch_thread_via_process_item() -> None:
    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config)
    item = FetchItem(
        priority=PRIORITY_SOCKET_EVENT,
        channel_id="C111",
        channel_name="general",
        thread_ts=_NOW,
    )
    await poller._process_item(item)
    assert len(poller.threads) == 1
    key = ("C111", _NOW)
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
    key = ("C111", _NOW)
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


@pytest.mark.asyncio
async def test_channel_watermark_set_after_fetch() -> None:
    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config)
    item = FetchItem(priority=PRIORITY_BACKFILL, channel_id="C111", channel_name="general")
    await poller._process_item(item)
    assert "C111" in poller.channel_watermarks
    assert poller.channel_watermarks["C111"] == _NOW


@pytest.mark.asyncio
async def test_thread_watermark_set_after_fetch() -> None:
    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config)
    item = FetchItem(
        priority=PRIORITY_BACKFILL,
        channel_id="C111",
        channel_name="general",
        thread_ts=_NOW,
    )
    await poller._process_item(item)
    assert ("C111", _NOW) in poller.thread_watermarks
    assert poller.thread_watermarks[("C111", _NOW)] == _REPLY_3


@pytest.mark.asyncio
async def test_refresh_passes_oldest_to_fetch_threads() -> None:
    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config)
    # Do initial backfill
    item = FetchItem(priority=PRIORITY_BACKFILL, channel_id="C111", channel_name="general")
    await poller._process_item(item)
    # Now do refresh
    refresh_item = FetchItem(priority=PRIORITY_REFRESH, channel_id="C111", channel_name="general")
    await poller._process_item(refresh_item)
    # Second call to fetch_threads should have oldest set
    calls = mock_slack.fetch_threads.call_args_list
    assert len(calls) == 2
    assert calls[1][1].get("oldest") == _NOW


@pytest.mark.asyncio
async def test_backfill_does_not_pass_oldest() -> None:
    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config)
    # Set a fake watermark
    poller._channel_watermarks["C111"] = "999.999"
    item = FetchItem(priority=PRIORITY_BACKFILL, channel_id="C111", channel_name="general")
    await poller._process_item(item)
    call_kwargs = mock_slack.fetch_threads.call_args[1]
    assert call_kwargs.get("oldest") is None


@pytest.mark.asyncio
async def test_incremental_merge_adds_participants() -> None:
    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config)
    # Initial full fetch
    item = FetchItem(
        priority=PRIORITY_BACKFILL,
        channel_id="C111",
        channel_name="general",
        thread_ts=_NOW,
    )
    await poller._process_item(item)
    key = ("C111", _NOW)
    assert poller.threads[key].reply_count == 3
    assert len(poller.threads[key].participants) == 3

    # Simulate incremental fetch with new reply from new user
    new_reply_ts = str(time.time() + 10)
    mock_slack.fetch_replies = AsyncMock(
        return_value=[
            {"ts": new_reply_ts, "user": "U_NEW", "text": "new reply"},
        ]
    )
    refresh_item = FetchItem(
        priority=PRIORITY_REFRESH,
        channel_id="C111",
        channel_name="general",
        thread_ts=_NOW,
    )
    await poller._process_item(refresh_item)
    assert poller.threads[key].reply_count == 4
    assert "U_NEW" in poller.threads[key].participants
    assert len(poller.threads[key].participants) == 4


@pytest.mark.asyncio
async def test_full_refresh_preserves_velocity_and_resurrection_state() -> None:
    """State merge contract: a full-fetch rebuild must carry forward velocity and
    resurrection fields, or every periodic refresh would silently reset them."""
    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config)
    item = FetchItem(
        priority=PRIORITY_BACKFILL,
        channel_id="C111",
        channel_name="general",
        thread_ts=_NOW,
    )
    await poller._process_item(item)
    key = ("C111", _NOW)
    entry = poller.threads[key]
    assert entry.first_seen_ts == float(_NOW)

    # Simulate accumulated velocity history + a resurrection event
    marker = time.time() - 100
    entry.resurrection_event_ts = marker
    entry.reply_timestamps = [time.time() - 5]

    # A full (non-incremental) refresh rebuilds the ThreadEntry from scratch
    await poller._fetch_thread("C111", "general", _NOW, incremental=False)
    rebuilt = poller.threads[key]
    assert rebuilt.resurrection_event_ts == marker
    assert rebuilt.first_seen_ts == float(_NOW)
    assert rebuilt.reply_timestamps  # carried forward + merged, not wiped


@pytest.mark.asyncio
async def test_incremental_merge_populates_reply_timestamps() -> None:
    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config)
    item = FetchItem(
        priority=PRIORITY_BACKFILL,
        channel_id="C111",
        channel_name="general",
        thread_ts=_NOW,
    )
    await poller._process_item(item)
    key = ("C111", _NOW)
    before = len(poller.threads[key].reply_timestamps)

    new_reply_ts = str(time.time() + 10)
    mock_slack.fetch_replies = AsyncMock(
        return_value=[{"ts": new_reply_ts, "user": "U_NEW", "text": "new reply"}]
    )
    refresh_item = FetchItem(
        priority=PRIORITY_REFRESH,
        channel_id="C111",
        channel_name="general",
        thread_ts=_NOW,
    )
    await poller._process_item(refresh_item)
    assert len(poller.threads[key].reply_timestamps) == before + 1


@pytest.mark.asyncio
async def test_dismiss_thread_evicts_and_filters(tmp_path) -> None:
    from slack_dashboard.dismiss import DismissStore

    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    store = DismissStore(tmp_path / "dismissed.jsonl")
    poller = SlackPoller(mock_slack, config, dismiss=store)
    item = FetchItem(priority=PRIORITY_BACKFILL, channel_id="C111", channel_name="general")
    await poller._process_item(item)
    key = ("C111", _NOW)
    assert key in poller.threads

    poller.dismiss_thread("C111", _NOW)
    assert key not in poller.threads
    assert store.is_dismissed("C111", _NOW)
    assert poller.ranked_threads() == []


@pytest.mark.asyncio
async def test_fetch_thread_skips_dismissed_before_fetch(tmp_path) -> None:
    from slack_dashboard.dismiss import DismissStore

    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    store = DismissStore(tmp_path / "dismissed.jsonl")
    store.dismiss("C111", _NOW)
    poller = SlackPoller(mock_slack, config, dismiss=store)
    await poller._fetch_thread("C111", "general", _NOW, incremental=False)
    # Dismissed: no thread created and no REST fetch burned
    assert ("C111", _NOW) not in poller.threads
    mock_slack.fetch_replies.assert_not_called()


@pytest.mark.asyncio
async def test_evict_threads_removes_dead(tmp_path) -> None:
    from datetime import UTC, datetime, timedelta

    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config)
    item = FetchItem(priority=PRIORITY_BACKFILL, channel_id="C111", channel_name="general")
    await poller._process_item(item)
    key = ("C111", _NOW)
    # Age the thread past max_thread_age_days with no resurrection
    poller.threads[key].last_activity = datetime.now(UTC) - timedelta(days=10)
    poller.threads[key].resurrection_event_ts = 0.0
    poller._evict_threads()
    assert key not in poller.threads


@pytest.mark.asyncio
async def test_fetch_channel_uses_per_channel_min_replies() -> None:
    from slack_dashboard.config import FetchConfig

    mock_slack = _make_mock_slack()
    config = AppConfig(
        channels={"incidents": "C111"},
        fetch=FetchConfig(min_replies=3, channel_min_replies={"incidents": 1}),
    )
    poller = SlackPoller(mock_slack, config)
    await poller._fetch_channel("C111", "incidents")
    # The high-weight ops channel resolves to min_replies=1, not the global 3
    assert mock_slack.fetch_threads.call_args[1]["min_replies"] == 1
