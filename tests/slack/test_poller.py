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
    # fetch_replies returns root + 3 replies = 4 messages; message_count = len = 4
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
    # message_count = len(replies) = 4 (root + 3)
    assert entry.message_count == 4
    assert len(entry.participants) == 3
    assert entry.channel_name == "general"
    assert entry.first_message == "root"


@pytest.mark.asyncio
async def test_participants_keyed_by_user_id_not_display_name() -> None:
    """All write paths must key participants by stable Slack user_id, not the resolved
    display name. The socket listener keys by raw user_id; if the REST path keyed by
    name, a user active via both paths would be counted twice under two different keys.
    See design 2026-06-27 Blocker 1. resolve_user returns a string distinct from the id
    here so any name-keying regression would surface as wrong keys."""
    mock_slack = _make_mock_slack()
    mock_slack.resolve_user = AsyncMock(side_effect=lambda uid: f"display-{uid}")
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config)
    await poller._process_item(
        FetchItem(priority=PRIORITY_BACKFILL, channel_id="C111", channel_name="general")
    )
    entry = poller.threads[("C111", _NOW)]
    # Participants keyed by raw user_id (U1 appears twice -> deduped to one key).
    assert set(entry.participants) == {"U1", "U2", "U3"}
    # started_by still uses the resolved display name for attribution, not the id.
    assert entry.started_by == "display-U1"


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
    assert entry.message_count == 4  # root + 3 replies
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
    # Initial full fetch: root + 3 replies = message_count=4, participants=3
    item = FetchItem(
        priority=PRIORITY_BACKFILL,
        channel_id="C111",
        channel_name="general",
        thread_ts=_NOW,
    )
    await poller._process_item(item)
    key = ("C111", _NOW)
    assert poller.threads[key].message_count == 4
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
    assert poller.threads[key].message_count == 5
    assert "U_NEW" in poller.threads[key].participants
    assert len(poller.threads[key].participants) == 4


@pytest.mark.asyncio
async def test_full_refresh_preserves_velocity_and_resurrection_state() -> None:
    """State merge contract: a full-fetch rebuild must carry forward velocity and
    resurrection fields, or every periodic refresh would silently reset them."""
    from slack_dashboard.thread import ReplyRecord

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
    # Set a reply record that should be carried forward via merge_replies
    entry.replies = [ReplyRecord(ts=time.time() - 5, author_id="U1", text="", is_root=False)]

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


@pytest.mark.asyncio
async def test_resurrection_reconstructed_without_prior_state() -> None:
    """Phase 6: a long-dead thread that comes back is a zombie even with no in-memory
    prior state (the eviction/restart case the carry-forward approach missed)."""
    import time

    from slack_dashboard.heat import is_zombie

    mock_slack = _make_mock_slack()
    now = time.time()
    old_parent = str(now - 5 * 86400)
    revive = str(now - 60)
    mock_slack.fetch_replies = AsyncMock(
        return_value=[
            {"ts": old_parent, "user": "U1", "text": "old root"},
            {"ts": str(now - 5 * 86400 + 30), "user": "U2", "text": "early reply"},
            {"ts": revive, "user": "U3", "text": "back from the dead"},
        ]
    )
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config)
    # No prior entry in _threads (simulates an evicted/cold thread)
    await poller._fetch_thread("C111", "general", old_parent, incremental=False)
    entry = poller.threads[("C111", old_parent)]
    assert entry.resurrection_event_ts == pytest.approx(float(revive))
    assert is_zombie(entry, config.heat, now)


@pytest.mark.asyncio
async def test_reconcile_refetches_only_changed_threads() -> None:
    """Phase 6 reconcile: re-fetch only threads whose latest_reply moved past the watermark."""
    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config)
    poller._channel_map = config.channels  # normally set by start(); reconcile iterates it
    # Seed one known thread via backfill (sets its watermark to _REPLY_3)
    await poller._process_item(
        FetchItem(priority=PRIORITY_BACKFILL, channel_id="C111", channel_name="general")
    )
    fetch_calls_before = mock_slack.fetch_replies.call_count

    # Channel listing: the known thread is unchanged (latest_reply == watermark),
    # plus a brand-new thread that must be picked up.
    new_ts = str(time.time() + 100)
    mock_slack.fetch_threads = AsyncMock(
        return_value=[
            {"ts": _NOW, "thread_ts": _NOW, "reply_count": 3, "latest_reply": _REPLY_3},
            {"ts": new_ts, "thread_ts": new_ts, "reply_count": 3, "latest_reply": new_ts},
        ]
    )
    mock_slack.fetch_replies = AsyncMock(
        return_value=[
            {"ts": new_ts, "user": "U1", "text": "new root"},
            {"ts": str(time.time() + 101), "user": "U2", "text": "r"},
        ]
    )
    await poller.reconcile()
    # Only the new/changed thread triggers a replies fetch; the unchanged one is skipped.
    assert mock_slack.fetch_replies.call_count == 1
    assert ("C111", new_ts) in poller.threads
    assert fetch_calls_before > 0  # sanity: backfill did fetch


@pytest.mark.asyncio
async def test_reconcile_refetches_old_parent_with_new_reply() -> None:
    """The literal discovery-hole case: a KNOWN old parent whose latest_reply advanced past
    its watermark must be re-fetched on reconcile (not just brand-new parents)."""
    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config)
    poller._channel_map = config.channels
    # Seed the thread; watermark becomes _REPLY_3
    await poller._process_item(
        FetchItem(priority=PRIORITY_BACKFILL, channel_id="C111", channel_name="general")
    )
    assert poller.thread_watermarks[("C111", _NOW)] == _REPLY_3

    # The SAME old parent now reports an advanced latest_reply (a new reply landed during a gap)
    advanced = str(time.time() + 500)
    mock_slack.fetch_threads = AsyncMock(
        return_value=[
            {"ts": _NOW, "thread_ts": _NOW, "reply_count": 4, "latest_reply": advanced},
        ]
    )
    mock_slack.fetch_replies = AsyncMock(
        return_value=[
            {"ts": _NOW, "user": "U1", "text": "root"},
            {"ts": advanced, "user": "U9", "text": "new reply on old parent"},
        ]
    )
    await poller.reconcile()
    # The old parent was re-fetched because latest_reply > watermark, and the watermark advanced
    assert mock_slack.fetch_replies.call_count == 1
    assert poller.thread_watermarks[("C111", _NOW)] == advanced


@pytest.mark.asyncio
async def test_velocity_not_double_counted_across_listener_and_fetch() -> None:
    """Integration: a socket append followed by a full fetch of the SAME reply must leave one
    timestamp, not two (the cross-component half of the velocity dedup fix)."""
    import time

    from slack_dashboard.config import HeatConfig
    from slack_dashboard.slack.listener import SocketListener

    now = time.time()
    parent = str(now - 7200)  # old parent, outside the velocity window
    reply = str(now - 60)  # recent reply, inside the window
    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"}, heat=HeatConfig(velocity_window_minutes=30))
    poller = SlackPoller(mock_slack, config)

    # Seed the entry via a first full fetch (parent only)
    mock_slack.fetch_replies = AsyncMock(
        return_value=[{"ts": parent, "user": "U1", "text": "root"}]
    )
    await poller._fetch_thread("C111", "general", parent, incremental=False)

    # Socket event for the new reply: listener merges a ReplyRecord
    listener = SocketListener(
        queue=poller.queue,
        threads=poller.threads,
        channel_ids={"C111"},
        channel_names={"C111": "general"},
        heat_config=config.heat,
    )
    listener._apply_event("C111", "general", parent, {"user": "U9", "ts": reply})
    entry = poller.threads[("C111", parent)]
    matches = [t for t in entry.reply_timestamps if abs(t - float(reply)) < 1e-6]
    assert len(matches) == 1

    # The socket-triggered full fetch returns the parent + the SAME reply again
    mock_slack.fetch_replies = AsyncMock(
        return_value=[
            {"ts": parent, "user": "U1", "text": "root"},
            {"ts": reply, "user": "U9", "text": "same reply via REST"},
        ]
    )
    await poller._fetch_thread("C111", "general", parent, incremental=False)
    entry = poller.threads[("C111", parent)]
    matches = [t for t in entry.reply_timestamps if abs(t - float(reply)) < 1e-6]
    assert len(matches) == 1  # deduped across listener + fetch, not double-counted


@pytest.mark.asyncio
async def test_evict_prunes_observed_by_horizon_not_age(tmp_path) -> None:
    """B1 regression: _evict_threads deletes observed rows for the EXACT evicted
    keys (last_activity horizon), and leaves a long-lived ACTIVE thread (old
    first_observed, recent last_activity) in the store. Pruning by a static
    first_observed age would purge the active thread and re-stamp it as falsely New."""
    from datetime import UTC, datetime, timedelta

    from slack_dashboard.observed import ObservedStore
    from slack_dashboard.thread import ThreadEntry

    observed = ObservedStore(tmp_path / "observed.db")
    observed.load()

    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config, observed=observed)

    # Active thread: stamped long ago (old first_observed) but recently active.
    active_key = ("C111", "active-old")
    observed.stamp(*active_key, now=1.0)
    poller.threads[active_key] = poller.threads.get(
        active_key,
        ThreadEntry(
            channel_id="C111",
            channel_name="general",
            thread_ts="active-old",
            first_message="m",
            started_by="U1",
            message_count=3,
            participants={"U1": 3},
            last_activity=datetime.now(UTC),  # recent activity -> not evicted
            first_observed_at=1.0,
        ),
    )

    # Dead thread: also old first_observed, but no recent activity -> evicted.
    dead_key = ("C111", "dead-old")
    observed.stamp(*dead_key, now=2.0)
    poller.threads[dead_key] = ThreadEntry(
        channel_id="C111",
        channel_name="general",
        thread_ts="dead-old",
        first_message="m",
        started_by="U1",
        message_count=3,
        participants={"U1": 3},
        last_activity=datetime.now(UTC) - timedelta(days=99),
        resurrection_event_ts=0.0,
        first_observed_at=2.0,
    )

    poller._evict_threads()

    # Dead thread evicted from memory AND its observed row deleted (re-stamp is fresh).
    assert dead_key not in poller.threads
    assert observed.stamp(*dead_key, now=500.0) == 500.0
    # Active thread untouched: still in memory, observed row preserved (no re-stamp).
    assert active_key in poller.threads
    assert observed.stamp(*active_key, now=500.0) == 1.0


@pytest.mark.asyncio
async def test_hot_path_does_no_sqlite_io(tmp_path) -> None:
    """The render hot path (ranked_threads) reads first_observed_at off ThreadEntry
    and never touches sqlite. Swap in a tripwire connection that fails on any query
    to prove zero sqlite I/O on the read path."""
    from slack_dashboard.observed import ObservedStore

    observed = ObservedStore(tmp_path / "observed.db")
    observed.load()
    mock_slack = _make_mock_slack()
    config = AppConfig(channels={"general": "C111"})
    poller = SlackPoller(mock_slack, config, observed=observed)

    # Populate threads (this DOES write to sqlite, on the creation chokepoint).
    item = FetchItem(priority=PRIORITY_BACKFILL, channel_id="C111", channel_name="general")
    await poller._process_item(item)

    class _TripwireConn:
        def execute(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("hot path issued a sqlite query")

        def executemany(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("hot path issued a sqlite query")

        def commit(self) -> None:
            raise AssertionError("hot path committed to sqlite")

    # sqlite3.Connection.execute is read-only, so swap the whole connection.
    observed._conn = _TripwireConn()  # type: ignore[assignment]
    ranked = poller.ranked_threads()
    assert ranked  # the threads still rank, with no sqlite touched
