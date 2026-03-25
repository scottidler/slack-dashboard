from datetime import UTC, datetime, timedelta

from slack_dashboard.config import HeatConfig
from slack_dashboard.heat import classify_tier, compute_heat, filter_stale_threads, rank_threads
from slack_dashboard.thread import ThreadEntry


def _make_thread(
    reply_count: int = 5,
    participants: int = 2,
    hours_ago: float = 0.5,
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
        last_activity=now - timedelta(hours=hours_ago),
    )


def test_compute_heat_recent_thread() -> None:
    config = HeatConfig()
    thread = _make_thread(reply_count=10, participants=3, hours_ago=0.0)
    score = compute_heat(thread, config)
    # base = (10 * 2) + (3 * 3) = 29, decay ~ 1.0
    assert abs(score - 29.0) < 1.0


def test_compute_heat_decays_with_age() -> None:
    config = HeatConfig()
    recent = _make_thread(reply_count=10, participants=3, hours_ago=0.0)
    old = _make_thread(reply_count=10, participants=3, hours_ago=12.0)
    recent_score = compute_heat(recent, config)
    old_score = compute_heat(old, config)
    assert recent_score > old_score
    # At 12 hours with 24h half-life: decay = 1.0 - (12/24) = 0.5
    # old_score ~ 29 * 0.5 = 14.5
    assert abs(old_score - 14.5) < 1.0


def test_compute_heat_near_zero_after_full_decay() -> None:
    config = HeatConfig()
    thread = _make_thread(reply_count=10, participants=3, hours_ago=24.0)
    score = compute_heat(thread, config)
    # decay = max(0.01, 1.0 - 24/24) = max(0.01, 0.0) = 0.01
    assert score < 1.0


def test_compute_heat_zero_replies() -> None:
    config = HeatConfig()
    thread = _make_thread(reply_count=0, participants=0, hours_ago=0.0)
    score = compute_heat(thread, config)
    assert score == 0.0


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


def test_rank_threads() -> None:
    config = HeatConfig()
    t1 = _make_thread(reply_count=5, participants=2, hours_ago=1, thread_ts="1")
    t2 = _make_thread(reply_count=20, participants=5, hours_ago=0, thread_ts="2")
    t3 = _make_thread(reply_count=1, participants=1, hours_ago=10, thread_ts="3")
    ranked = rank_threads([t1, t2, t3], config)
    assert ranked[0].thread_ts == "2"
    assert ranked[1].thread_ts == "1"
    assert ranked[2].thread_ts == "3"
    assert ranked[0].heat_score > ranked[1].heat_score > ranked[2].heat_score


def test_filter_stale_threads() -> None:
    config = HeatConfig(max_thread_age_days=3)
    now = datetime.now(UTC)
    active = _make_thread(reply_count=5, participants=2, hours_ago=1, thread_ts="active")
    stale = ThreadEntry(
        channel_id="C123",
        channel_name="general",
        thread_ts="stale",
        first_message="Old",
        reply_count=5,
        participants={"U1"},
        last_activity=now - timedelta(days=4),
    )
    result = filter_stale_threads([active, stale], config)
    assert len(result) == 1
    assert result[0].thread_ts == "active"


def test_high_reply_old_thread_scores_low() -> None:
    """The key insight of the redesign: old threads with many replies score near zero."""
    config = HeatConfig()
    old_hot = _make_thread(reply_count=200, participants=20, hours_ago=25)
    new_warm = _make_thread(reply_count=10, participants=3, hours_ago=0)
    old_score = compute_heat(old_hot, config)
    new_score = compute_heat(new_warm, config)
    assert new_score > old_score
