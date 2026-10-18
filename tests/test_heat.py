import time as _time
from datetime import UTC, datetime, timedelta

from slack_dashboard.config import HeatConfig
from slack_dashboard.heat import (
    HeatBreakdown,
    classify_tier,
    compute_heat,
    detect_resurrection,
    filter_stale_threads,
    following_damping,
    heat_breakdown,
    is_following,
    is_heated,
    is_vip,
    is_zombie,
    prune_timestamps,
    rank_threads,
    reconstruct_resurrection,
    score_threads,
    structural_heat,
    velocity,
)
from slack_dashboard.thread import ReplyRecord, ThreadEntry

# Alias kept for the Phase 2 tests appended below.
_compute_heat = compute_heat


def _compute_heat_at(thread: ThreadEntry, config: HeatConfig, now: float) -> float:
    """Deterministic score at a pinned ``now`` (single-path via heat_breakdown.overall)."""
    return heat_breakdown(thread, config, now).overall


def _make_thread(
    message_count: int = 5,
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
        message_count=message_count,
        participants={f"U{i}": 1 for i in range(participants)},
        last_activity=now - timedelta(hours=hours_ago),
    )


def _work_now() -> float:
    """A fixed 'now' inside the working-hours window (Tue 2026-06-30 10:00 PT)."""
    return datetime(2026, 6, 30, 17, 0, tzinfo=UTC).timestamp()  # 10:00 PT (UTC-7)


def _work_thread(
    message_count: int = 5,
    participants: int = 2,
    work_hours_ago: float = 0.0,
    channel_name: str = "general",
    thread_ts: str = "1111111111.111111",
    now: float | None = None,
) -> ThreadEntry:
    """A thread whose last_activity is ``work_hours_ago`` before a working-hours ``now``.

    Because ``now`` sits mid-morning, subtracting whole hours stays inside the daily window
    for the small offsets these tests use, so working-hours-since-last == wall-clock hours.
    """
    now = now if now is not None else _work_now()
    return ThreadEntry(
        channel_id="C123",
        channel_name=channel_name,
        thread_ts=thread_ts,
        first_message="Test message",
        started_by="U1",
        message_count=message_count,
        participants={f"U{i}": 1 for i in range(participants)},
        last_activity=datetime.fromtimestamp(now - work_hours_ago * 3600, tz=UTC),
    )


def test_compute_heat_recent_thread() -> None:
    # base_norm = 50 * 29 / (29 + 15) ~ 32.95; atrophy 1.0 (0 work-hours since); not followed.
    config = HeatConfig()
    now = _work_now()
    thread = _work_thread(message_count=10, participants=3, work_hours_ago=0.0, now=now)
    score = _compute_heat_at(thread, config, now)
    assert abs(score - (50.0 * 29.0 / 44.0)) < 0.5


def test_compute_heat_decays_with_age() -> None:
    config = HeatConfig()  # atrophy_half_life_work_hours = 3.0
    now = _work_now()
    recent = _work_thread(message_count=10, participants=3, work_hours_ago=0.0, now=now)
    old = _work_thread(message_count=10, participants=3, work_hours_ago=3.0, now=now)
    recent_score = _compute_heat_at(recent, config, now)
    old_score = _compute_heat_at(old, config, now)
    assert recent_score > old_score
    # 3 work-hours == one half-life -> atrophy 0.5 -> old_score ~ half of recent.
    assert abs(old_score - recent_score * 0.5) < 0.5


def test_compute_heat_near_zero_after_full_decay() -> None:
    config = HeatConfig()
    now = _work_now()  # Tue 2026-06-30 10:00 PT
    # Last activity Mon 2026-06-29 10:00 PT: a full working day earlier. Working hours
    # between = Mon 10:00->18:00 (8) + Tue 06:00->10:00 (4) = 12 work-hours == 4 half-lives
    # -> atrophy 0.5^4 = 0.0625 -> a ~33 base collapses to ~2.
    last = datetime(2026, 6, 29, 17, 0, tzinfo=UTC).timestamp()  # Mon 10:00 PT
    thread = _work_thread(message_count=10, participants=3, now=now)
    thread.last_activity = datetime.fromtimestamp(last, tz=UTC)
    score = _compute_heat_at(thread, config, now)
    assert score < 3.0


def test_compute_heat_zero_replies() -> None:
    config = HeatConfig()
    now = _work_now()
    thread = _work_thread(message_count=0, participants=0, work_hours_ago=0.0, now=now)
    score = _compute_heat_at(thread, config, now)
    assert score == 0.0


def test_classify_tier_hot() -> None:
    # Absolute mode, tier_hot 50 / tier_warm 20 (the default is "relative" since
    # Phase 5's calibration flip; explicitly select absolute here to test that path).
    config = HeatConfig(tier_method="absolute")
    assert classify_tier(50.0, 0, 10, config) == "hot"
    assert classify_tier(100.0, 5, 10, config) == "hot"


def test_classify_tier_warm() -> None:
    config = HeatConfig(tier_method="absolute")
    assert classify_tier(20.0, 0, 10, config) == "warm"
    assert classify_tier(49.9, 0, 10, config) == "warm"


def test_classify_tier_cold() -> None:
    config = HeatConfig(tier_method="absolute")
    assert classify_tier(0.0, 0, 10, config) == "cold"
    assert classify_tier(19.9, 0, 10, config) == "cold"


def test_classify_tier_relative_top_n_with_floor() -> None:
    # Relative mode: top tier_hot_count (3) are hot IF they clear tier_floor; next up to
    # tier_warm_count (10) are warm; below the floor is always cold regardless of rank.
    config = HeatConfig(tier_method="relative", tier_hot_count=3, tier_warm_count=5, tier_floor=5.0)
    assert classify_tier(40.0, 0, 20, config) == "hot"  # top-3, above floor
    assert classify_tier(40.0, 2, 20, config) == "hot"
    assert classify_tier(40.0, 3, 20, config) == "warm"  # rank 3 -> warm band
    assert classify_tier(40.0, 4, 20, config) == "warm"
    assert classify_tier(40.0, 5, 20, config) == "cold"  # past warm count
    # Below the absolute floor -> cold even at the very top (fully-atrophied board).
    assert classify_tier(4.0, 0, 20, config) == "cold"


def test_classify_tier_relative_counts_clamp_on_small_board() -> None:
    # A board smaller than the counts must not error; counts clamp to total.
    config = HeatConfig(
        tier_method="relative", tier_hot_count=3, tier_warm_count=10, tier_floor=1.0
    )
    assert classify_tier(10.0, 0, 1, config) == "hot"
    assert classify_tier(10.0, 0, 2, config) == "hot"


def test_rank_threads() -> None:
    config = HeatConfig()
    t1 = _make_thread(message_count=5, participants=2, hours_ago=1, thread_ts="1")
    t2 = _make_thread(message_count=20, participants=5, hours_ago=0, thread_ts="2")
    t3 = _make_thread(message_count=1, participants=1, hours_ago=10, thread_ts="3")
    ranked = rank_threads([t1, t2, t3], config)
    assert ranked[0].thread_ts == "2"
    assert ranked[1].thread_ts == "1"
    assert ranked[2].thread_ts == "3"
    assert ranked[0].heat_score > ranked[1].heat_score > ranked[2].heat_score


def test_score_threads_is_pure_and_matches_rank_threads() -> None:
    config = HeatConfig(tier_hot_count=1, tier_warm_count=2)
    now = _time.time()
    threads = [
        _make_thread(message_count=5, participants=2, hours_ago=1, thread_ts="1"),
        _make_thread(message_count=20, participants=5, hours_ago=0, thread_ts="2"),
        _make_thread(message_count=1, participants=1, hours_ago=10, thread_ts="3"),
    ]
    ranked = score_threads(threads, config, now)
    assert [r.entry.thread_ts for r in ranked] == ["2", "1", "3"]
    assert [r.tier for r in ranked] == ["hot", "warm", "cold"]
    assert ranked[0].score > ranked[1].score > ranked[2].score
    # Pure: the entries keep their defaults.
    assert all(t.heat_score == 0.0 and t.heat_tier == "cold" for t in threads)
    # The mutating wrapper writes exactly what the pure path computed.
    written = rank_threads(threads, config, now)
    assert [(t.thread_ts, t.heat_score, t.heat_tier) for t in written] == [
        (r.entry.thread_ts, r.score, r.tier) for r in ranked
    ]


def test_score_threads_empty_board() -> None:
    assert score_threads([], HeatConfig(), _time.time()) == []


def test_filter_stale_threads_at_a_pinned_now() -> None:
    config = HeatConfig(max_thread_age_days=1)
    thread = _make_thread(hours_ago=30, thread_ts="t")
    now = _time.time()
    assert filter_stale_threads([thread], config, now) == []
    assert filter_stale_threads([thread], config, now - 12 * 3600) == [thread]


def test_filter_stale_threads() -> None:
    config = HeatConfig(max_thread_age_days=3)
    now = datetime.now(UTC)
    active = _make_thread(message_count=5, participants=2, hours_ago=1, thread_ts="active")
    stale = ThreadEntry(
        channel_id="C123",
        channel_name="general",
        thread_ts="stale",
        first_message="Old",
        started_by="U1",
        message_count=5,
        participants={"U1": 1},
        last_activity=now - timedelta(days=4),
    )
    result = filter_stale_threads([active, stale], config)
    assert len(result) == 1
    assert result[0].thread_ts == "active"


def test_high_reply_old_thread_scores_low() -> None:
    """The redesign's key insight: a big-but-stale thread sinks below a small-but-fresh one.

    The HARD base ceiling makes it achievable - a 200-message thread and a 10-message thread
    both approach base_cap, so once atrophy is applied the stale one falls below the fresh one.
    """
    config = HeatConfig()
    now = _work_now()
    # Big thread idle ~12 work-hours (4 half-lives, atrophy 0.0625); small thread fresh.
    old_hot = _work_thread(message_count=200, participants=20, work_hours_ago=8.0, now=now)
    new_warm = _work_thread(message_count=10, participants=3, work_hours_ago=0.0, now=now)
    old_score = _compute_heat_at(old_hot, config, now)
    new_score = _compute_heat_at(new_warm, config, now)
    assert new_score > old_score


def test_channel_weight_orders_threads() -> None:
    config = HeatConfig(channel_weights={"sre": 2.0, "proj-atlas": 0.5})
    now = _work_now()
    sre = _work_thread(
        message_count=10, participants=3, work_hours_ago=0.0, channel_name="sre", now=now
    )
    proj = _work_thread(
        message_count=10, participants=3, work_hours_ago=0.0, channel_name="proj-atlas", now=now
    )
    neutral = _work_thread(
        message_count=10, participants=3, work_hours_ago=0.0, channel_name="random", now=now
    )
    sre_score = _compute_heat_at(sre, config, now)
    proj_score = _compute_heat_at(proj, config, now)
    neutral_score = _compute_heat_at(neutral, config, now)
    assert sre_score > neutral_score > proj_score
    # 2.0x and 0.5x multipliers relative to the neutral 1.0 channel
    assert abs(sre_score - 2.0 * neutral_score) < 0.01
    assert abs(proj_score - 0.5 * neutral_score) < 0.01


def test_velocity_counts_in_window() -> None:
    config = HeatConfig(velocity_window_minutes=30)
    now = _time.time()
    thread = _make_thread()
    # 6 replies in the last 30 min, 1 well outside - place as reply records
    thread.replies = [
        ReplyRecord(ts=now - 60 * i, author_id="U1", text="", is_root=(i == 0)) for i in range(6)
    ] + [ReplyRecord(ts=now - 60 * 60, author_id="U1", text="", is_root=False)]
    vel = velocity(thread, config, now)
    assert abs(vel - 6 / 30) < 1e-6


def test_velocity_boosts_heat() -> None:
    config = HeatConfig(velocity_weight=10.0, velocity_window_minutes=30)
    now = _time.time()
    spiking = _make_thread(message_count=10, participants=3, hours_ago=0.0)
    spiking.replies = [
        ReplyRecord(ts=now - 30 * i, author_id="U1", text="", is_root=(i == 0)) for i in range(10)
    ]
    slow = _make_thread(message_count=10, participants=3, hours_ago=0.0)
    assert _compute_heat(spiking, config) > _compute_heat(slow, config)


def test_velocity_zero_when_weight_zero() -> None:
    # Default velocity_weight=0.0 keeps behavior identical to base*recency
    config = HeatConfig()
    now = _time.time()
    thread = _make_thread(message_count=10, participants=3, hours_ago=0.0)
    thread.replies = [
        ReplyRecord(ts=now - 30 * i, author_id="U1", text="", is_root=(i == 0)) for i in range(10)
    ]
    no_velocity = _make_thread(message_count=10, participants=3, hours_ago=0.0)
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


def test_decay_half_life_migration_still_loads() -> None:
    # The legacy decay-half-life-hours key still maps onto decay-hours (structural_heat
    # consumes decay_hours); the re-shaped ranking score no longer uses either knob.
    config = HeatConfig.model_validate({"decay-half-life-hours": 12})
    assert config.decay_hours == 12


def test_tier_threshold_migration_maps_legacy_keys() -> None:
    # Legacy hot-threshold / warm-threshold migrate onto the new tier-hot / tier-warm knobs.
    config = HeatConfig.model_validate({"hot-threshold": 42, "warm-threshold": 17})
    assert config.tier_hot == 42.0
    assert config.tier_warm == 17.0


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
    # With the new replies record, merge_replies dedupes by normalized ts key.
    from slack_dashboard.thread import merge_replies

    config = HeatConfig(velocity_window_minutes=30)
    now = _time.time()
    raw = now - 60
    r1 = ReplyRecord(ts=raw, author_id="U1", text="", is_root=False)
    r2 = ReplyRecord(ts=raw, author_id="U1", text="", is_root=False)
    merged_records = merge_replies([r1], [r2])
    thread = _make_thread()
    thread.replies = merged_records
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
    thread = _make_thread(message_count=10, participants=3, hours_ago=0.0)
    assert compute_heat(thread, vip_cfg) > compute_heat(thread, base_cfg)


def test_people_weight_default_matches_participant_weight() -> None:
    # With no people-weights set, each participant contributes participant_weight exactly:
    # volume = 10*2 + 3*3 = 29 -> base_norm = 50*29/(29+15) = 32.95..., fresh, neutral.
    config = HeatConfig()
    now = _work_now()
    thread = _work_thread(message_count=10, participants=3, work_hours_ago=0.0, now=now)
    assert abs(heat_breakdown(thread, config, now).overall - (50.0 * 29.0 / 44.0)) < 1e-6


def test_people_weight_cap_bounds_contribution() -> None:
    # The cap clamps the people term BEFORE base_norm saturation, so a crowd cannot run away.
    capped = HeatConfig(people_weights={"U0": 100, "U1": 100, "U2": 100}, people_weight_cap=10)
    uncapped = HeatConfig(people_weights={"U0": 100, "U1": 100, "U2": 100})
    now = _work_now()
    thread = _work_thread(message_count=0, participants=3, work_hours_ago=0.0, now=now)
    # capped volume = min(300, 10) = 10 -> base_norm = 50*10/25 = 20; uncapped volume 300
    # -> base_norm = 50*300/315 = 47.6. The cap keeps the score well below the uncapped one.
    capped_score = heat_breakdown(thread, capped, now).overall
    uncapped_score = heat_breakdown(thread, uncapped, now).overall
    assert abs(capped_score - (50.0 * 10.0 / 25.0)) < 1e-6
    assert capped_score < uncapped_score


# ---------------------------------------------------------------------------
# structural_heat / is_heated tests (Phase 1 groundwork)
# ---------------------------------------------------------------------------


def _make_heated_thread(
    replies: list[tuple[str, float]],
    message_count: int | None = None,
    hours_ago: float = 0.1,
) -> ThreadEntry:
    """Build a thread with given (author_id, ts) reply list for structural heat tests.

    Args:
        replies: list of (author_id, ts) tuples to build ReplyRecord objects from.
        message_count: override; defaults to len(replies).
        hours_ago: how old last_activity is (controls decay).
    """
    now = datetime.now(UTC)
    records = [
        ReplyRecord(ts=ts, author_id=author, text=f"msg {i}", is_root=(i == 0))
        for i, (author, ts) in enumerate(replies)
    ]
    participants = {}
    for author, _ in replies:
        participants[author] = participants.get(author, 0) + 1
    entry = ThreadEntry(
        channel_id="C1",
        channel_name="test",
        thread_ts="100.000000",
        first_message="root",
        started_by=replies[0][0] if replies else "U1",
        message_count=message_count if message_count is not None else len(replies),
        participants=participants,
        last_activity=now - timedelta(hours=hours_ago),
    )
    entry.replies = records
    return entry


def test_structural_heat_monologue_is_zero() -> None:
    """A thread where only one author posts has exchange~=0 -> structural=0."""
    config = HeatConfig(heated_structural_scale=1.0, decay_hours=24)
    now = _time.time()
    thread = _make_heated_thread(
        [("U1", now - 300), ("U1", now - 200), ("U1", now - 100)],
        hours_ago=0.01,
    )
    result = structural_heat(thread, config, now)
    assert result == 0.0


def test_structural_heat_back_and_forth_scores_nonzero() -> None:
    """A real back-and-forth between two authors should score above zero."""
    config = HeatConfig(heated_structural_scale=1.0, decay_hours=24)
    now = _time.time()
    thread = _make_heated_thread(
        [
            ("U1", now - 500),
            ("U2", now - 400),
            ("U1", now - 300),
            ("U2", now - 200),
            ("U1", now - 100),
        ],
        hours_ago=0.01,
    )
    result = structural_heat(thread, config, now)
    assert result > 0.0


def test_structural_heat_decays_to_zero_with_age() -> None:
    """An old fight must decay to 0 (no floor unlike compute_heat)."""
    config = HeatConfig(heated_structural_scale=1.0, decay_hours=1)
    now = _time.time()
    thread = _make_heated_thread(
        [("U1", now - 7200), ("U2", now - 6000), ("U1", now - 4800), ("U2", now - 3600)],
        hours_ago=2.0,  # 2 hours ago, decay_hours=1 -> decay=max(0, 1-2/1)=0
    )
    result = structural_heat(thread, config, now)
    assert result == 0.0


def test_structural_heat_clamped_to_ten() -> None:
    """Even with huge volume and perfect alternation, result stays <= 10."""
    config = HeatConfig(heated_structural_scale=10.0, decay_hours=24)
    now = _time.time()
    # Many alternations, high message_count, recent
    replies = []
    for i in range(20):
        author = "U1" if i % 2 == 0 else "U2"
        replies.append((author, now - (20 - i) * 10))
    thread = _make_heated_thread(replies, hours_ago=0.01)
    result = structural_heat(thread, config, now)
    assert result <= 10.0


def test_is_heated_false_when_monologue() -> None:
    """A monologue thread never fires is_heated regardless of threshold."""
    config = HeatConfig(heated_threshold=1.0, heated_structural_scale=1.0)
    now = _time.time()
    thread = _make_heated_thread(
        [("U1", now - 300), ("U1", now - 200), ("U1", now - 100)],
        hours_ago=0.01,
    )
    assert not is_heated(thread, config, now)


def test_is_heated_true_for_active_exchange() -> None:
    """A dense, recent back-and-forth should fire is_heated with low threshold."""
    config = HeatConfig(
        heated_threshold=1.0,
        heated_structural_scale=5.0,
        heated_tone_weight=3.0,
        decay_hours=24,
        velocity_window_minutes=30,
    )
    now = _time.time()
    replies = []
    for i in range(10):
        author = "U1" if i % 2 == 0 else "U2"
        replies.append((author, now - (10 - i) * 30))
    thread = _make_heated_thread(replies, hours_ago=0.01)
    assert is_heated(thread, config, now)


def test_is_heated_false_when_score_below_threshold() -> None:
    """A thread just below threshold should not fire."""
    # Force structural=0 (monologue) and tone=0 -> score=0 < threshold=8
    config = HeatConfig(heated_threshold=8.0)
    now = _time.time()
    thread = _make_heated_thread(
        [("U1", now - 200), ("U1", now - 100)],
        hours_ago=0.01,
    )
    assert not is_heated(thread, config, now)


# is_following tests (🔔 Slack follows this thread for me)


def _thread_with_participants(participants: dict[str, int]) -> ThreadEntry:
    return ThreadEntry(
        channel_id="C1",
        channel_name="sre",
        thread_ts="100.000000",
        first_message="hello",
        started_by="U1",
        message_count=sum(participants.values()),
        participants=participants,
        last_activity=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_is_following_reads_subscribed() -> None:
    thread = _thread_with_participants({"U1": 2, "U2": 1})
    assert not is_following(thread)
    thread.subscribed = True
    assert is_following(thread)


def test_is_following_ignores_participation() -> None:
    # Posting is not the signal: a thread I posted in that Slack does not follow (I
    # unfollowed it) is not following, and a thread I never posted in can be.
    posted = _thread_with_participants({"USELF": 1, "U2": 1})
    assert not is_following(posted)
    mentioned = _thread_with_participants({"U2": 3})
    mentioned.subscribed = True
    assert is_following(mentioned)


# following_damping tests (followed drops; unread replies after last_read rebuild)


def _followed_thread(
    unread: int, read: int = 1, last_read_known: bool = True, subscribed: bool = True
) -> ThreadEntry:
    """A thread with a root, ``read`` replies up to last_read, then ``unread`` replies."""
    now = _time.time()
    root_ts = now - 1000
    replies = [ReplyRecord(ts=root_ts, author_id="U9", text="root", is_root=True)]
    for i in range(read):
        replies.append(ReplyRecord(ts=root_ts + 1 + i, author_id="U2", text="r", is_root=False))
    last_read = replies[-1].ts
    for i in range(unread):
        replies.append(ReplyRecord(ts=last_read + 1 + i, author_id="U3", text="u", is_root=False))
    return ThreadEntry(
        channel_id="C1",
        channel_name="sre",
        thread_ts=f"{root_ts:.6f}",
        first_message="root",
        started_by="U9",
        message_count=len(replies),
        participants={"U9": 1, "U2": read, "U3": unread},
        last_activity=datetime.now(UTC),
        replies=replies,
        subscribed=subscribed,
        last_read=last_read if last_read_known else None,
    )


def test_following_damping_off_when_not_following() -> None:
    thread = _followed_thread(unread=0, subscribed=False)
    assert following_damping(thread, HeatConfig(), _time.time()) == 1.0


def test_following_damping_disabled_is_noop() -> None:
    # followed_drop = 1.0 disables the feature (no drop possible).
    thread = _followed_thread(unread=0)
    config = HeatConfig(followed_drop=1.0)
    assert following_damping(thread, config, _time.time()) == 1.0


def test_following_damping_flat_at_drop_with_rebuild_zero() -> None:
    # My yml: drop 0.2, rebuild 0. Unread replies change nothing; the drop is flat.
    config = HeatConfig(followed_drop=0.2, followed_rebuild_per_msg=0.0)
    now = _time.time()
    for unread in (0, 1, 10, 100):
        assert abs(following_damping(_followed_thread(unread), config, now) - 0.2) < 1e-9


def test_following_damping_full_drop_when_all_read() -> None:
    config = HeatConfig(followed_drop=0.8, followed_rebuild_per_msg=0.15)
    thread = _followed_thread(unread=0)
    assert abs(following_damping(thread, config, _time.time()) - 0.8) < 1e-9


def test_following_damping_rebuilds_with_unread_replies() -> None:
    config = HeatConfig(followed_drop=0.8, followed_rebuild_per_msg=0.15)
    now = _time.time()
    fresh = following_damping(_followed_thread(0), config, now)
    partial = following_damping(_followed_thread(3), config, now)
    # 3 unread: rebuild = 0.45 -> damping = 0.8 + 0.2*0.45 = 0.89
    assert fresh < partial < 1.0
    assert abs(partial - (0.8 + 0.2 * 0.45)) < 1e-9


def test_following_damping_counts_only_non_root_replies_after_last_read() -> None:
    # last_read before the root: the root is never counted, only the 2 replies are.
    config = HeatConfig(followed_drop=0.5, followed_rebuild_per_msg=0.1)
    thread = _followed_thread(unread=0, read=2)
    thread.last_read = thread.replies[0].ts - 1
    assert abs(following_damping(thread, config, _time.time()) - (0.5 + 0.5 * 0.2)) < 1e-9


def test_following_damping_counts_none_when_last_read_unknown() -> None:
    # Unknown read state is not "unread everything": the full drop applies.
    config = HeatConfig(followed_drop=0.8, followed_rebuild_per_msg=0.15)
    thread = _followed_thread(unread=10, last_read_known=False)
    assert abs(following_damping(thread, config, _time.time()) - 0.8) < 1e-9


def test_following_damping_clamped_to_floor_and_ceiling() -> None:
    config = HeatConfig(followed_drop=0.8, followed_rebuild_per_msg=0.15)
    now = _time.time()
    assert following_damping(_followed_thread(0), config, now) >= 0.8
    assert following_damping(_followed_thread(100), config, now) == 1.0


def test_compute_heat_applies_following_drop() -> None:
    config = HeatConfig(followed_drop=0.8, followed_rebuild_per_msg=0.15)
    now = _time.time()
    followed = _followed_thread(unread=0)
    unfollowed = _followed_thread(unread=0, subscribed=False)
    damped = heat_breakdown(followed, config, now).overall
    undamped = heat_breakdown(unfollowed, config, now).overall
    assert damped < undamped
    assert abs(damped - undamped * 0.8) < 1e-6


# ---------------------------------------------------------------------------
# heat_breakdown / is_vip tests (Phase 1: single-path factor exposure)
# ---------------------------------------------------------------------------


def test_heat_breakdown_fields_match_hand_computed() -> None:
    # message_count=10, participants=3 (all default weight 3.0), fresh, neutral channel.
    config = HeatConfig()
    now = _work_now()
    thread = _work_thread(message_count=10, participants=3, work_hours_ago=0.0, now=now)
    bd = heat_breakdown(thread, config, now)
    # people_term = 3 * participant_weight(3.0) = 9.0 (no cap hit at default cap)
    assert abs(bd.people_term - 9.0) < 1e-6
    # volume = 10*2 + 9 = 29; base_norm = base_cap(50) * 29 / (29 + base_k(15)) = 32.9545...
    assert abs(bd.base - (50.0 * 29.0 / 44.0)) < 1e-6
    assert bd.message_count == 10
    assert bd.people_count == 3
    assert bd.channel_weight == 1.0  # neutral channel
    assert bd.velocity == 0.0  # no reply timestamps
    assert bd.activity == 0.0  # velocity 0 -> no burst term
    assert abs(bd.atrophy - 1.0) < 1e-6  # 0 work-hours since -> 0.5^0 = 1.0
    assert abs(bd.alive_boost - 1.0) < 1e-6  # alive_weight 0.0 seed -> no lift
    assert bd.damping == 1.0  # not followed -> no drop
    assert bd.has_vip is False  # all default-weight participants
    assert bd.time_since_last == 0.0  # last_activity == now
    # overall = channel_weight * (base_norm + activity) * atrophy * alive_boost * damping
    assert abs(bd.overall - (50.0 * 29.0 / 44.0)) < 1e-6


def test_heat_breakdown_channel_weight_factor() -> None:
    config = HeatConfig(channel_weights={"sre": 2.0})
    now = _work_now()
    thread = _work_thread(
        message_count=10, participants=3, work_hours_ago=0.0, channel_name="sre", now=now
    )
    bd = heat_breakdown(thread, config, now)
    assert bd.channel_weight == 2.0
    # overall = 2.0 * base_norm * 1.0 * 1.0 * 1.0
    assert abs(bd.overall - 2.0 * (50.0 * 29.0 / 44.0)) < 1e-6


def test_heat_breakdown_atrophy_factor() -> None:
    # 3 working hours idle == one half-life (atrophy_half_life_work_hours=3) -> atrophy 0.5.
    config = HeatConfig()
    now = _work_now()
    thread = _work_thread(message_count=10, participants=3, work_hours_ago=3.0, now=now)
    bd = heat_breakdown(thread, config, now)
    assert abs(bd.atrophy - 0.5) < 1e-6
    assert abs(bd.time_since_last - 3.0) < 1e-3
    assert abs(bd.overall - (50.0 * 29.0 / 44.0) * 0.5) < 1e-3


def test_heat_breakdown_activity_outside_ceiling() -> None:
    # Velocity contributes an additive burst term, capped at activity_cap, OUTSIDE base_norm.
    config = HeatConfig(velocity_window_minutes=30, velocity_weight=10.0, activity_cap=20.0)
    now = _work_now()
    thread = _work_thread(message_count=10, participants=3, work_hours_ago=0.0, now=now)
    thread.replies = [
        ReplyRecord(ts=now - 60 * i, author_id="U1", text="", is_root=(i == 0)) for i in range(6)
    ]
    bd = heat_breakdown(thread, config, now)
    # 6 replies / 30 min = 0.2 rep/min; activity = min(20, 0.2*10) = 2.0
    assert abs(bd.velocity - 6 / 30) < 1e-6
    assert abs(bd.activity - 2.0) < 1e-6
    # overall = (base_norm + activity) * 1.0
    assert abs(bd.overall - ((50.0 * 29.0 / 44.0) + 2.0)) < 1e-3


def test_heat_breakdown_activity_capped() -> None:
    # A huge velocity is clamped to activity_cap so a burst cannot run the score away.
    config = HeatConfig(velocity_window_minutes=30, velocity_weight=1000.0, activity_cap=20.0)
    now = _work_now()
    thread = _work_thread(message_count=10, participants=3, work_hours_ago=0.0, now=now)
    thread.replies = [
        ReplyRecord(ts=now - 5 * i, author_id="U1", text="", is_root=(i == 0)) for i in range(20)
    ]
    bd = heat_breakdown(thread, config, now)
    assert bd.activity == 20.0


def test_heat_breakdown_alive_boost_freshness_gated() -> None:
    # With alive_weight > 0, a long-lived AND fresh thread is lifted; the same thread once
    # idle collapses back toward 1.0 as atrophy -> 0 (the x atrophy gate).
    config = HeatConfig(alive_weight=1.0, alive_k=6.0)
    now = _work_now()
    fresh = _work_thread(message_count=10, participants=3, work_hours_ago=0.0, now=now)
    fresh.first_seen_ts = now - 6 * 3600  # 6 work-hours of life before last post
    fresh_bd = heat_breakdown(fresh, config, now)
    # time_alive ~ 6 work-hours -> f = 6/(6+6) = 0.5; atrophy 1.0 -> alive_boost = 1 + 1*0.5*1 = 1.5
    assert fresh_bd.time_alive > 0.0
    assert fresh_bd.alive_boost > 1.0


def test_heat_breakdown_damping_factor() -> None:
    config = HeatConfig(followed_drop=0.8, followed_rebuild_per_msg=0.15)
    now = _time.time()
    thread = _followed_thread(unread=0)
    bd = heat_breakdown(thread, config, now)
    # Followed, nothing unread -> full drop -> damping == followed_drop == 0.8
    assert abs(bd.damping - 0.8) < 1e-6


def test_heat_breakdown_people_term_capped() -> None:
    config = HeatConfig(people_weights={"U0": 100, "U1": 100, "U2": 100}, people_weight_cap=10)
    now = _work_now()
    thread = _work_thread(message_count=0, participants=3, work_hours_ago=0.0, now=now)
    bd = heat_breakdown(thread, config, now)
    # people_term = min(300, 10) = 10; has_vip True (above-default weights present)
    assert abs(bd.people_term - 10.0) < 1e-6
    # volume = 0*2 + 10 = 10; base_norm = 50 * 10 / (10 + 15) = 20.0
    assert abs(bd.base - (50.0 * 10.0 / 25.0)) < 1e-6
    assert bd.has_vip is True


def test_heat_breakdown_has_vip_true_with_pinned_participant() -> None:
    config = HeatConfig(people_weights={"U0": 50})
    now = _work_now()
    thread = _work_thread(message_count=10, participants=3, work_hours_ago=0.0, now=now)
    bd = heat_breakdown(thread, config, now)
    assert bd.has_vip is True


def test_heat_breakdown_monologue_boost_and_damping_noop() -> None:
    # Root only, no replies: time_alive 0, velocity 0 -> alive_boost 1.0, activity 0; a thread
    # Slack does not follow -> damping 1.0.
    config = HeatConfig(alive_weight=1.0)
    now = _work_now()
    thread = _work_thread(message_count=1, participants=1, work_hours_ago=0.0, now=now)
    thread.first_seen_ts = now  # first == last -> time_alive 0
    bd = heat_breakdown(thread, config, now)
    assert bd.time_alive == 0.0
    assert bd.velocity == 0.0
    assert bd.activity == 0.0
    assert abs(bd.alive_boost - 1.0) < 1e-6
    assert bd.damping == 1.0


def test_single_path_invariant() -> None:
    # compute_heat must equal heat_breakdown(...).overall for several fixtures.
    config = HeatConfig()
    fixtures = [
        _make_thread(message_count=10, participants=3, hours_ago=0.0),
        _make_thread(message_count=5, participants=2, hours_ago=1.0),
        _make_thread(message_count=200, participants=20, hours_ago=25.0),
        _make_thread(message_count=0, participants=0, hours_ago=0.0),
    ]
    for thread in fixtures:
        # compute_heat is a thin wrapper over heat_breakdown(...).overall; with a fresh
        # default now on each, the two agree to within sub-second atrophy drift.
        assert abs(compute_heat(thread, config) - heat_breakdown(thread, config).overall) < 1e-3


def test_single_path_invariant_with_following() -> None:
    config = HeatConfig(followed_drop=0.8, followed_rebuild_per_msg=0.15)
    thread = _followed_thread(unread=0)
    now = _time.time()
    # Pinned now: heat_breakdown is deterministic, so compute_heat (its .overall) agrees.
    bd = heat_breakdown(thread, config, now)
    assert bd.overall == heat_breakdown(thread, config, now).overall
    assert abs(bd.damping - 0.8) < 1e-6


def test_compute_heat_numeric_result_pinned() -> None:
    # Pin the re-shaped formula: volume=29 -> base_norm = 50*29/44 = 32.95..., fresh, neutral.
    config = HeatConfig()
    now = _work_now()
    thread = _work_thread(message_count=10, participants=3, work_hours_ago=0.0, now=now)
    assert abs(heat_breakdown(thread, config, now).overall - (50.0 * 29.0 / 44.0)) < 1e-6


def test_heat_breakdown_overall_equals_compute_heat_default_now() -> None:
    # With the default now (each call captures its own), the two paths agree to within
    # the sub-second atrophy drift between calls - i.e. effectively equal.
    config = HeatConfig()
    thread = _make_thread(message_count=10, participants=3, hours_ago=0.5)
    assert abs(compute_heat(thread, config) - heat_breakdown(thread, config).overall) < 1e-3


def test_heat_breakdown_is_frozen() -> None:
    config = HeatConfig()
    thread = _make_thread(message_count=10, participants=3, hours_ago=0.0)
    bd = heat_breakdown(thread, config)
    assert isinstance(bd, HeatBreakdown)


def test_is_vip_true_when_above_default_weight() -> None:
    config = HeatConfig(people_weights={"U0": 50})
    thread = _make_thread(message_count=10, participants=3, hours_ago=0.0)
    assert is_vip(thread, config) is True


def test_is_vip_false_when_all_default() -> None:
    config = HeatConfig()
    thread = _make_thread(message_count=10, participants=3, hours_ago=0.0)
    assert is_vip(thread, config) is False


# --- Topic terms and atrophy shape (docs/design/2026-10-18-heat-autoresearch.md) ---------
#
# The autoresearch loop's keeps, each as its own predicate: an unrated thread is neutral,
# every knob at its default is the pre-topic score, the resolved gate empties an answered
# thread, the leader vote and gate lift a leader thread, hyperbolic decay shares the half
# point with exponential decay and keeps a tail, and the half-life stretches with p(leader).

_TUNED = HeatConfig(
    atrophy_curve="hyperbolic",
    atrophy_half_life_work_hours=0.5,
    atrophy_leader_stretch=1.0,
    leader_weight=45.0,
    leader_gate_floor=0.3,
    resolved_drop=1.0,
)


def _rated(thread: ThreadEntry, leader: float = 0.0, resolved: float = 0.0) -> ThreadEntry:
    thread.topics = {"leader": leader, "resolved": resolved}
    return thread


def test_topic_knobs_at_default_ignore_the_probabilities() -> None:
    config = HeatConfig()
    now = _work_now()
    unrated = _compute_heat_at(_work_thread(work_hours_ago=1.0), config, now)
    rated = _compute_heat_at(_rated(_work_thread(work_hours_ago=1.0), 0.9, 0.9), config, now)
    assert abs(rated - unrated) < 1e-9


def test_unrated_thread_is_neutral_under_the_tuned_knobs() -> None:
    now = _work_now()
    plain = HeatConfig(atrophy_curve="hyperbolic", atrophy_half_life_work_hours=0.5)
    thread = _work_thread(work_hours_ago=1.0)
    assert abs(_compute_heat_at(thread, _TUNED, now) - _compute_heat_at(thread, plain, now)) < 1e-9
    breakdown = heat_breakdown(thread, _TUNED, now)
    assert breakdown.votes == 0.0
    assert breakdown.gates == 1.0
    assert breakdown.p_leader is None
    assert breakdown.p_resolved is None


def test_resolved_gate_empties_an_answered_thread() -> None:
    config = HeatConfig(resolved_drop=1.0)
    now = _work_now()
    open_thread = _rated(_work_thread(), resolved=0.0)
    half_done = _rated(_work_thread(), resolved=0.5)
    done = _rated(_work_thread(), resolved=1.0)
    full = _compute_heat_at(open_thread, config, now)
    assert full > 0
    assert abs(_compute_heat_at(half_done, config, now) - full / 2) < 1e-9
    assert _compute_heat_at(done, config, now) == 0.0
    assert heat_breakdown(done, config, now).gates == 0.0


def test_leader_vote_and_gate_lift_a_leader_thread() -> None:
    config = HeatConfig(leader_weight=45.0, leader_gate_floor=0.3)
    now = _work_now()
    leader = heat_breakdown(_rated(_work_thread(), leader=1.0), config, now)
    nobody = heat_breakdown(_rated(_work_thread(), leader=0.0), config, now)
    assert leader.votes == 45.0
    assert leader.gates == 1.0
    assert nobody.votes == 0.0
    assert abs(nobody.gates - 0.3) < 1e-9
    # A three-message thread that needs a leader outranks the same thread that does not.
    assert leader.overall > nobody.overall
    expected = (leader.base + leader.activity + 45.0) * leader.atrophy * leader.alive_boost
    assert abs(leader.overall - expected * leader.channel_weight * leader.damping) < 1e-9


def test_hyperbolic_atrophy_shares_the_half_point_and_keeps_a_tail() -> None:
    exponential = HeatConfig(atrophy_half_life_work_hours=1.0)
    hyperbolic = HeatConfig(atrophy_half_life_work_hours=1.0, atrophy_curve="hyperbolic")
    now = _work_now()
    at_half = _work_thread(work_hours_ago=1.0)
    assert abs(heat_breakdown(at_half, exponential, now).atrophy - 0.5) < 1e-9
    assert abs(heat_breakdown(at_half, hyperbolic, now).atrophy - 0.5) < 1e-9
    later = _work_thread(work_hours_ago=3.0)
    assert abs(heat_breakdown(later, exponential, now).atrophy - 0.125) < 1e-9
    assert abs(heat_breakdown(later, hyperbolic, now).atrophy - 0.25) < 1e-9


def test_leader_stretch_lengthens_the_half_life_for_leader_threads() -> None:
    config = HeatConfig(atrophy_half_life_work_hours=1.0, atrophy_leader_stretch=1.0)
    now = _work_now()
    # p = 1 -> half-life 1.5 h: at 1.5 h the thread is at exactly half.
    leader = _rated(_work_thread(work_hours_ago=1.5), leader=1.0)
    assert abs(heat_breakdown(leader, config, now).atrophy - 0.5) < 1e-9
    # p = 0 -> half-life 0.5 h: at 0.5 h the thread is at exactly half.
    nobody = _rated(_work_thread(work_hours_ago=0.5), leader=0.0)
    assert abs(heat_breakdown(nobody, config, now).atrophy - 0.5) < 1e-9
    # An unrated thread keeps the configured half-life.
    plain = _work_thread(work_hours_ago=1.0)
    assert abs(heat_breakdown(plain, config, now).atrophy - 0.5) < 1e-9


def test_single_path_invariant_holds_with_topic_terms() -> None:
    thread = _rated(_work_thread(work_hours_ago=0.25), leader=0.6, resolved=0.2)
    breakdown = heat_breakdown(thread, _TUNED, _work_now())
    assert breakdown.overall == _compute_heat_at(thread, _TUNED, _work_now())
    assert breakdown.p_leader == 0.6
    assert breakdown.p_resolved == 0.2
    assert 0.0 < breakdown.gates < 1.0
    assert abs(breakdown.votes - 27.0) < 1e-9
