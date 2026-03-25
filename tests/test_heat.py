from datetime import UTC, datetime, timedelta

from slack_dashboard.config import HeatConfig, PollingConfig, PruningConfig
from slack_dashboard.heat import classify_tier, compute_heat, prune_threads, rank_threads
from slack_dashboard.thread import ThreadEntry


def _make_thread(
    reply_count: int = 5,
    participants: int = 2,
    minutes_ago: float = 10,
    channel_name: str = "general",
    thread_ts: str = "1111111111.111111",
) -> ThreadEntry:
    now = datetime.now(UTC)
    return ThreadEntry(
        channel_id="C123",
        channel_name=channel_name,
        thread_ts=thread_ts,
        first_message="Test message",
        reply_count=reply_count,
        participants={f"U{i}" for i in range(participants)},
        last_activity=now - timedelta(minutes=minutes_ago),
    )


def test_compute_heat_basic() -> None:
    config = HeatConfig()
    thread = _make_thread(reply_count=10, participants=3, minutes_ago=5)
    score = compute_heat(thread, config)
    # (10 * 2) + (3 * 3) + max(0, 100 - 5) = 20 + 9 + 95 ~ 124
    assert abs(score - 124.0) < 1.0


def test_compute_heat_zero_replies() -> None:
    config = HeatConfig()
    thread = _make_thread(reply_count=0, participants=0, minutes_ago=200)
    score = compute_heat(thread, config)
    # (0 * 2) + (0 * 3) + max(0, 100 - 200) = 0 + 0 + 0 = 0
    assert score == 0.0


def test_compute_heat_recency_decay() -> None:
    config = HeatConfig()
    recent = _make_thread(reply_count=5, participants=2, minutes_ago=0)
    old = _make_thread(reply_count=5, participants=2, minutes_ago=100)
    recent_score = compute_heat(recent, config)
    old_score = compute_heat(old, config)
    assert recent_score > old_score
    # old recency_bonus = max(0, 100 - 100) = 0
    assert old_score == (5 * 2) + (2 * 3) + 0  # 16


def test_classify_tier_hot() -> None:
    config = HeatConfig()
    assert classify_tier(50.0, config) == "hot"
    assert classify_tier(100.0, config) == "hot"


def test_classify_tier_warm() -> None:
    config = HeatConfig()
    assert classify_tier(20.0, config) == "warm"
    assert classify_tier(49.9, config) == "warm"


def test_classify_tier_cold() -> None:
    config = HeatConfig()
    assert classify_tier(0.0, config) == "cold"
    assert classify_tier(19.9, config) == "cold"


def test_classify_tier_cold_demotion_by_time() -> None:
    config = HeatConfig()
    polling = PollingConfig()
    # Thread has high heat score but inactive for > cold_threshold_minutes
    thread = _make_thread(reply_count=50, participants=10, minutes_ago=120)
    score = compute_heat(thread, config)
    assert score > config.hot_threshold
    tier = classify_tier(
        score, config, minutes_inactive=120, cold_threshold_minutes=polling.cold_threshold_minutes
    )
    assert tier == "cold"


def test_rank_threads() -> None:
    config = HeatConfig()
    polling = PollingConfig()
    t1 = _make_thread(reply_count=5, participants=2, minutes_ago=10, thread_ts="1")
    t2 = _make_thread(reply_count=20, participants=5, minutes_ago=5, thread_ts="2")
    t3 = _make_thread(reply_count=1, participants=1, minutes_ago=50, thread_ts="3")
    ranked = rank_threads([t1, t2, t3], config, polling)
    assert ranked[0].thread_ts == "2"
    assert ranked[1].thread_ts == "1"
    assert ranked[2].thread_ts == "3"
    assert ranked[0].heat_score > ranked[1].heat_score > ranked[2].heat_score


def test_prune_threads() -> None:
    pruning = PruningConfig(cold_max_hours=24)
    now = datetime.now(UTC)
    active = _make_thread(reply_count=5, participants=2, minutes_ago=60, thread_ts="active")
    stale = ThreadEntry(
        channel_id="C123",
        channel_name="general",
        thread_ts="stale",
        first_message="Old",
        reply_count=5,
        participants={"U1"},
        last_activity=now - timedelta(hours=25),
    )
    result = prune_threads([active, stale], pruning)
    assert len(result) == 1
    assert result[0].thread_ts == "active"
