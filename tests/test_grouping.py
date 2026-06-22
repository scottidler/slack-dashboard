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


def test_deep_link_uses_app_scheme_when_team_id_set() -> None:
    # A team id wins over the web forms: open the native desktop app, keeping the
    # dotted ts so it lands on the exact thread.
    assert deep_link("tatari", "C123", "1718900000.000100", "T999") == (
        "slack://channel?team=T999&id=C123&message=1718900000.000100"
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


def test_group_by_none_single_unlabeled_group() -> None:
    # group-by=none: one label-less group in heat (input) order, no headers.
    config = AppConfig()
    threads = [
        _thread(channel_name="sre", thread_ts="1"),
        _thread(channel_name="data", thread_ts="2"),
    ]
    groups = group_threads(threads, "none", config)
    assert len(groups) == 1
    assert groups[0].label == ""
    assert len(groups[0].rows) == 2


def test_group_by_size_buckets() -> None:
    config = AppConfig()
    threads = [
        _thread(thread_ts="1", reply_count=120),  # huge
        _thread(thread_ts="2", reply_count=60),  # large
        _thread(thread_ts="3", reply_count=30),  # medium
        _thread(thread_ts="4", reply_count=10),  # small
        _thread(thread_ts="5", reply_count=3),  # small
    ]
    groups = group_threads(threads, "size", config)
    assert [g.label for g in groups] == [
        "huge (100+)",
        "large (50-99)",
        "medium (25-49)",
        "small (3-24)",
    ]
    # Heat (input) order is preserved within a bucket.
    assert [r.reply_count for r in groups[-1].rows] == [10, 3]


def test_group_by_size_drops_empty_buckets() -> None:
    config = AppConfig()
    threads = [_thread(thread_ts="1", reply_count=5), _thread(thread_ts="2", reply_count=8)]
    groups = group_threads(threads, "size", config)
    assert [g.label for g in groups] == ["small (3-24)"]


def test_group_by_velocity_buckets() -> None:
    config = AppConfig()
    now = time.time()
    spiking = _thread(thread_ts="1")
    spiking.reply_timestamps = [now - i for i in range(20)]  # 20 replies in-window
    active = _thread(thread_ts="2")
    active.reply_timestamps = [now - 60, now - 120, now - 180]  # 3 in-window
    idle = _thread(thread_ts="3")
    idle.reply_timestamps = []
    groups = group_threads([spiking, active, idle], "velocity", config)
    assert [g.label for g in groups] == ["spiking (15+)", "active (1-14)", "idle (0)"]


def test_group_by_invalid_falls_back_to_none() -> None:
    config = AppConfig()
    threads = [_thread(channel_name="sre")]
    groups = group_threads(threads, "bogus", config)
    assert groups[0].label == ""
    assert len(groups[0].rows) == 1


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
