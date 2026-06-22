import time
from datetime import UTC, datetime

from slack_dashboard.config import AppConfig
from slack_dashboard.thread import ThreadEntry
from slack_dashboard.web import _build_row, deep_link, group_threads


def _thread(
    channel_name: str = "general",
    thread_ts: str = "1.1",
    reply_count: int = 5,
    participants: int = 2,
    heat_tier: str = "warm",
) -> ThreadEntry:
    return ThreadEntry(
        channel_id="C" + channel_name,
        channel_name=channel_name,
        thread_ts=thread_ts,
        first_message="msg",
        started_by="U1",
        reply_count=reply_count,
        participants={f"U{i}": 1 for i in range(participants)},
        last_activity=datetime.now(UTC),
        heat_tier=heat_tier,
    )


def test_deep_link_strips_dot() -> None:
    assert deep_link("tatari", "C123", "1718900000.000100") == (
        "https://tatari.slack.com/archives/C123/p1718900000000100"
    )


def test_group_by_channel_partitions() -> None:
    config = AppConfig()
    threads = [
        _thread(channel_name="sre", thread_ts="1"),
        _thread(channel_name="sre", thread_ts="2"),
        _thread(channel_name="data", thread_ts="3"),
    ]
    groups = group_threads(threads, "channel", config)
    labels = [g.label for g in groups]
    assert labels == ["sre", "data"]
    assert len(groups[0].rows) == 2
    assert len(groups[1].rows) == 1


def test_group_by_size_sorts_descending() -> None:
    config = AppConfig()
    threads = [
        _thread(thread_ts="1", reply_count=3),
        _thread(thread_ts="2", reply_count=30),
        _thread(thread_ts="3", reply_count=10),
    ]
    groups = group_threads(threads, "size", config)
    assert len(groups) == 1
    counts = [r.reply_count for r in groups[0].rows]
    assert counts == [30, 10, 3]


def test_group_by_participants_sorts_descending() -> None:
    config = AppConfig()
    threads = [
        _thread(thread_ts="1", participants=2),
        _thread(thread_ts="2", participants=5),
    ]
    groups = group_threads(threads, "participants", config)
    counts = [r.participant_count for r in groups[0].rows]
    assert counts == [5, 2]


def test_group_by_invalid_falls_back_to_channel() -> None:
    config = AppConfig()
    threads = [_thread(channel_name="sre")]
    groups = group_threads(threads, "bogus", config)
    assert groups[0].label == "sre"


def test_build_row_emits_fire_for_hot() -> None:
    config = AppConfig()
    row = _build_row(_thread(heat_tier="hot"), config)
    assert "\N{FIRE}" in row.emojis


def test_build_row_emits_zombie_for_resurrected() -> None:
    config = AppConfig()
    thread = _thread(heat_tier="warm")
    now = time.time()
    thread.first_seen_ts = now - 5 * 86400
    thread.resurrection_event_ts = now - 3600
    row = _build_row(thread, config)
    assert "\N{ZOMBIE}" in row.emojis


def test_build_row_no_emoji_when_cold_and_not_zombie() -> None:
    config = AppConfig()
    row = _build_row(_thread(heat_tier="cold"), config)
    assert row.emojis == ""


def test_deep_link_fallback_when_workspace_empty() -> None:
    assert deep_link("", "C123", "1718900000.000100") == (
        "https://slack.com/app_redirect?channel=C123&message_ts=1718900000.000100"
    )
