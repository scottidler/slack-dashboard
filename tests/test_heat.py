import time as _time
from datetime import UTC, datetime, timedelta

from slack_dashboard.config import HeatConfig
from slack_dashboard.heat import (
    classify_tier,
    compute_heat,
    detect_resurrection,
    filter_stale_threads,
    is_zombie,
    prune_timestamps,
    rank_threads,
    reconstruct_resurrection,
    velocity,
)
from slack_dashboard.thread import ThreadEntry

# Alias kept for the Phase 2 tests appended below.
_compute_heat = compute_heat


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
        started_by="U1",
        reply_count=reply_count,
        participants={f"U{i}": 1 for i in range(participants)},
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
        started_by="U1",
        reply_count=5,
        participants={"U1": 1},
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


def test_channel_weight_orders_threads() -> None:
    config = HeatConfig(channel_weights={"sre": 2.0, "proj-atlas": 0.5})
    sre = _make_thread(reply_count=10, participants=3, hours_ago=0.0, channel_name="sre")
    proj = _make_thread(reply_count=10, participants=3, hours_ago=0.0, channel_name="proj-atlas")
    neutral = _make_thread(reply_count=10, participants=3, hours_ago=0.0, channel_name="random")
    sre_score = _compute_heat(sre, config)
    proj_score = _compute_heat(proj, config)
    neutral_score = _compute_heat(neutral, config)
    assert sre_score > neutral_score > proj_score
    # 2.0x and 0.5x multipliers relative to the neutral 1.0 channel
    assert abs(sre_score - 2.0 * neutral_score) < 0.01
    assert abs(proj_score - 0.5 * neutral_score) < 0.01


def test_velocity_counts_in_window() -> None:
    config = HeatConfig(velocity_window_minutes=30)
    now = _time.time()
    thread = _make_thread()
    # 6 replies in the last 30 min, 1 well outside
    thread.reply_timestamps = [now - 60 * i for i in range(6)] + [now - 60 * 60]
    vel = velocity(thread, config, now)
    assert abs(vel - 6 / 30) < 1e-6


def test_velocity_boosts_heat() -> None:
    config = HeatConfig(velocity_weight=10.0, velocity_window_minutes=30)
    now = _time.time()
    spiking = _make_thread(reply_count=10, participants=3, hours_ago=0.0)
    spiking.reply_timestamps = [now - 30 * i for i in range(10)]
    slow = _make_thread(reply_count=10, participants=3, hours_ago=0.0)
    slow.reply_timestamps = []
    assert _compute_heat(spiking, config) > _compute_heat(slow, config)


def test_velocity_zero_when_weight_zero() -> None:
    # Default velocity_weight=0.0 keeps behavior identical to base*recency
    config = HeatConfig()
    now = _time.time()
    thread = _make_thread(reply_count=10, participants=3, hours_ago=0.0)
    thread.reply_timestamps = [now - 30 * i for i in range(10)]
    no_velocity = _make_thread(reply_count=10, participants=3, hours_ago=0.0)
    assert abs(_compute_heat(thread, config) - _compute_heat(no_velocity, config)) < 0.01


def test_detect_resurrection_trips_on_large_gap() -> None:
    config = HeatConfig(resurrection_gap_hours=24)
    now = _time.time()
    prior = now - 48 * 3600
    assert detect_resurrection(prior, now, config) is True


def test_detect_resurrection_no_trip_on_small_gap() -> None:
    config = HeatConfig(resurrection_gap_hours=24)
    now = _time.time()
    prior = now - 2 * 3600
    assert detect_resurrection(prior, now, config) is False


def test_detect_resurrection_no_trip_without_prior() -> None:
    config = HeatConfig(resurrection_gap_hours=24)
    assert detect_resurrection(0.0, _time.time(), config) is False


def test_is_zombie_true_for_recent_revival_of_old_thread() -> None:
    config = HeatConfig(resurrection_age_days=2, resurrection_display_hours=24)
    now = _time.time()
    thread = _make_thread()
    thread.first_seen_ts = now - 5 * 86400  # 5 days old
    thread.resurrection_event_ts = now - 1 * 3600  # revived an hour ago
    assert is_zombie(thread, config, now) is True


def test_is_zombie_clears_after_display_window() -> None:
    config = HeatConfig(resurrection_age_days=2, resurrection_display_hours=24)
    now = _time.time()
    thread = _make_thread()
    thread.first_seen_ts = now - 5 * 86400
    thread.resurrection_event_ts = now - 48 * 3600  # revived 2 days ago, past display window
    assert is_zombie(thread, config, now) is False


def test_is_zombie_false_for_young_thread() -> None:
    config = HeatConfig(resurrection_age_days=2, resurrection_display_hours=24)
    now = _time.time()
    thread = _make_thread()
    thread.first_seen_ts = now - 1 * 3600  # young
    thread.resurrection_event_ts = now - 1 * 3600
    assert is_zombie(thread, config, now) is False


def test_is_zombie_false_without_event() -> None:
    config = HeatConfig()
    thread = _make_thread()
    assert is_zombie(thread, config) is False


def test_decay_rename_equivalence() -> None:
    # decay_hours=24 + decay_floor=0.01 reproduces the prior half-life-named behavior
    config = HeatConfig(decay_hours=24, decay_floor=0.01)
    thread = _make_thread(reply_count=10, participants=3, hours_ago=12.0)
    score = _compute_heat(thread, config)
    # base=29, decay=1-12/24=0.5 -> 14.5
    assert abs(score - 14.5) < 1.0


def test_prune_timestamps_dedups_by_normalized_key() -> None:
    # Same reply recorded twice with a sub-ulp difference (socket round-trip vs raw fetch)
    config = HeatConfig(velocity_window_minutes=30)
    now = _time.time()
    raw = now - 60
    round_tripped = datetime.fromtimestamp(raw, tz=UTC).timestamp()
    pruned = prune_timestamps([raw, round_tripped], config, now)
    assert len(pruned) == 1


def test_prune_timestamps_keeps_distinct() -> None:
    config = HeatConfig(velocity_window_minutes=30)
    now = _time.time()
    pruned = prune_timestamps([now - 60, now - 120, now - 180], config, now)
    assert len(pruned) == 3


def test_velocity_not_double_counted_after_socket_plus_fetch() -> None:
    # Simulates the race: listener appended raw ts, full fetch merges the same ts again.
    config = HeatConfig(velocity_window_minutes=30)
    now = _time.time()
    raw = now - 60
    merged = prune_timestamps([raw] + [raw], config, now)
    thread = _make_thread()
    thread.reply_timestamps = merged
    assert velocity(thread, config, now) == 1 / 30


def test_reconstruct_resurrection_finds_gap() -> None:
    config = HeatConfig(resurrection_gap_hours=24)
    now = _time.time()
    # parent + early replies, then a 48h-quiet gap, then a reviving reply
    ts = [now - 5 * 86400, now - 5 * 86400 + 60, now - 60]
    event = reconstruct_resurrection(sorted(ts), config)
    assert event == now - 60


def test_reconstruct_resurrection_no_gap() -> None:
    config = HeatConfig(resurrection_gap_hours=24)
    now = _time.time()
    ts = [now - 180, now - 120, now - 60]
    assert reconstruct_resurrection(sorted(ts), config) == 0.0


def test_reconstruct_resurrection_picks_most_recent_gap() -> None:
    config = HeatConfig(resurrection_gap_hours=24)
    now = _time.time()
    # two qualifying gaps; the most recent reviving reply should win
    ts = [now - 10 * 86400, now - 8 * 86400, now - 3 * 86400, now - 120]
    event = reconstruct_resurrection(sorted(ts), config)
    assert event == now - 120


def test_people_weight_raises_score() -> None:
    # A pinned participant (above-default weight) lifts the score vs an all-default thread.
    base_cfg = HeatConfig()
    vip_cfg = HeatConfig(people_weights={"U0": 50})
    thread = _make_thread(reply_count=10, participants=3, hours_ago=0.0)
    assert compute_heat(thread, vip_cfg) > compute_heat(thread, base_cfg)


def test_people_weight_default_matches_participant_weight() -> None:
    # With no people-weights set, each participant contributes participant_weight exactly,
    # so the score is identical to the pre-Phase-2 formula.
    config = HeatConfig()
    thread = _make_thread(reply_count=10, participants=3, hours_ago=0.0)
    # base = 10*2 + 3*3 = 29 (decay ~1.0)
    assert abs(compute_heat(thread, config) - 29.0) < 1.0


def test_people_weight_cap_bounds_contribution() -> None:
    # The cap clamps the total people term so a crowd of weighted people cannot run away.
    capped = HeatConfig(people_weights={"U0": 100, "U1": 100, "U2": 100}, people_weight_cap=10)
    uncapped = HeatConfig(people_weights={"U0": 100, "U1": 100, "U2": 100})
    thread = _make_thread(reply_count=0, participants=3, hours_ago=0.0)
    # capped people_term = min(300, 10) = 10; uncapped = 300
    assert abs(compute_heat(thread, capped) - 10.0) < 0.5
    assert abs(compute_heat(thread, uncapped) - 300.0) < 5.0
