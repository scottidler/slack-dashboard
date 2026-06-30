import time as _time
from datetime import UTC, datetime, timedelta

from slack_dashboard.config import HeatConfig
from slack_dashboard.heat import (
    classify_tier,
    compute_heat,
    detect_resurrection,
    filter_stale_threads,
    involvement_damping,
    is_heated,
    is_involved,
    is_zombie,
    prune_timestamps,
    rank_threads,
    reconstruct_resurrection,
    structural_heat,
    velocity,
)
from slack_dashboard.thread import ReplyRecord, ThreadEntry

# Alias kept for the Phase 2 tests appended below.
_compute_heat = compute_heat


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


def test_compute_heat_recent_thread() -> None:
    config = HeatConfig()
    thread = _make_thread(message_count=10, participants=3, hours_ago=0.0)
    score = compute_heat(thread, config)
    # base = (10 * 2) + (3 * 3) = 29, decay ~ 1.0
    assert abs(score - 29.0) < 1.0


def test_compute_heat_decays_with_age() -> None:
    config = HeatConfig()
    recent = _make_thread(message_count=10, participants=3, hours_ago=0.0)
    old = _make_thread(message_count=10, participants=3, hours_ago=12.0)
    recent_score = compute_heat(recent, config)
    old_score = compute_heat(old, config)
    assert recent_score > old_score
    # At 12 hours with 24h half-life: decay = 1.0 - (12/24) = 0.5
    # old_score ~ 29 * 0.5 = 14.5
    assert abs(old_score - 14.5) < 1.0


def test_compute_heat_near_zero_after_full_decay() -> None:
    config = HeatConfig()
    thread = _make_thread(message_count=10, participants=3, hours_ago=24.0)
    score = compute_heat(thread, config)
    # decay = max(0.01, 1.0 - 24/24) = max(0.01, 0.0) = 0.01
    assert score < 1.0


def test_compute_heat_zero_replies() -> None:
    config = HeatConfig()
    thread = _make_thread(message_count=0, participants=0, hours_ago=0.0)
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
    t1 = _make_thread(message_count=5, participants=2, hours_ago=1, thread_ts="1")
    t2 = _make_thread(message_count=20, participants=5, hours_ago=0, thread_ts="2")
    t3 = _make_thread(message_count=1, participants=1, hours_ago=10, thread_ts="3")
    ranked = rank_threads([t1, t2, t3], config)
    assert ranked[0].thread_ts == "2"
    assert ranked[1].thread_ts == "1"
    assert ranked[2].thread_ts == "3"
    assert ranked[0].heat_score > ranked[1].heat_score > ranked[2].heat_score


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
    """The key insight of the redesign: old threads with many replies score near zero."""
    config = HeatConfig()
    old_hot = _make_thread(message_count=200, participants=20, hours_ago=25)
    new_warm = _make_thread(message_count=10, participants=3, hours_ago=0)
    old_score = compute_heat(old_hot, config)
    new_score = compute_heat(new_warm, config)
    assert new_score > old_score


def test_channel_weight_orders_threads() -> None:
    config = HeatConfig(channel_weights={"sre": 2.0, "proj-atlas": 0.5})
    sre = _make_thread(message_count=10, participants=3, hours_ago=0.0, channel_name="sre")
    proj = _make_thread(message_count=10, participants=3, hours_ago=0.0, channel_name="proj-atlas")
    neutral = _make_thread(message_count=10, participants=3, hours_ago=0.0, channel_name="random")
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


def test_decay_rename_equivalence() -> None:
    # decay_hours=24 + decay_floor=0.01 reproduces the prior half-life-named behavior
    config = HeatConfig(decay_hours=24, decay_floor=0.01)
    thread = _make_thread(message_count=10, participants=3, hours_ago=12.0)
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
    # With no people-weights set, each participant contributes participant_weight exactly,
    # so the score is identical to the pre-Phase-2 formula.
    config = HeatConfig()
    thread = _make_thread(message_count=10, participants=3, hours_ago=0.0)
    # base = 10*2 + 3*3 = 29 (decay ~1.0)
    assert abs(compute_heat(thread, config) - 29.0) < 1.0


def test_people_weight_cap_bounds_contribution() -> None:
    # The cap clamps the total people term so a crowd of weighted people cannot run away.
    capped = HeatConfig(people_weights={"U0": 100, "U1": 100, "U2": 100}, people_weight_cap=10)
    uncapped = HeatConfig(people_weights={"U0": 100, "U1": 100, "U2": 100})
    thread = _make_thread(message_count=0, participants=3, hours_ago=0.0)
    # capped people_term = min(300, 10) = 10; uncapped = 300
    assert abs(compute_heat(thread, capped) - 10.0) < 0.5
    assert abs(compute_heat(thread, uncapped) - 300.0) < 5.0


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


# is_involved tests (👤 the current user has posted in this thread)


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


def test_is_involved_true_when_self_in_participants() -> None:
    thread = _thread_with_participants({"U1": 2, "U2": 1})
    assert is_involved(thread, "U1")


def test_is_involved_false_when_self_not_participant() -> None:
    thread = _thread_with_participants({"U1": 2, "U2": 1})
    assert not is_involved(thread, "UZZZ")


def test_is_involved_false_when_self_unresolved() -> None:
    # None self_user_id (auth.test failed / not yet run) never matches.
    thread = _thread_with_participants({"U1": 2})
    assert not is_involved(thread, None)


# involvement_damping tests (recent self-post lowers priority, fades as it is buried)


def _involved_thread(self_ago_s: float, messages_after: int, self_id: str = "U1") -> ThreadEntry:
    now = _time.time()
    my_ts = now - self_ago_s
    replies = [ReplyRecord(ts=my_ts, author_id=self_id, text="me", is_root=False)]
    for i in range(messages_after):
        replies.append(ReplyRecord(ts=my_ts + (i + 1), author_id="UOTHER", text="x", is_root=False))
    thread = ThreadEntry(
        channel_id="C1",
        channel_name="sre",
        thread_ts="100.000000",
        first_message="root",
        started_by="U9",
        message_count=1 + messages_after,
        participants={self_id: 1, "UOTHER": messages_after},
        last_activity=datetime.now(UTC),
    )
    thread.replies = replies
    return thread


def test_involvement_damping_none_self_is_noop() -> None:
    thread = _involved_thread(self_ago_s=0, messages_after=0)
    assert involvement_damping(thread, HeatConfig(), None, _time.time()) == 1.0


def test_involvement_damping_disabled_is_noop() -> None:
    thread = _involved_thread(self_ago_s=0, messages_after=0)
    config = HeatConfig(involved_damping=0.0)
    assert involvement_damping(thread, config, "U1", _time.time()) == 1.0


def test_involvement_damping_noop_when_user_has_no_post() -> None:
    thread = _involved_thread(self_ago_s=0, messages_after=0, self_id="U1")
    # The user "UZZZ" never posted, so there is nothing to damp.
    assert involvement_damping(thread, HeatConfig(), "UZZZ", _time.time()) == 1.0


def test_involvement_damping_strongest_right_after_post() -> None:
    # Just posted (0s ago), nothing after -> freshness 1 -> damping = 1 - involved_damping.
    config = HeatConfig(involved_damping=0.5, involved_decay_messages=10, involved_decay_hours=24)
    thread = _involved_thread(self_ago_s=0, messages_after=0)
    assert abs(involvement_damping(thread, config, "U1", _time.time()) - 0.5) < 1e-6


def test_involvement_damping_fades_as_messages_pile_up() -> None:
    config = HeatConfig(involved_damping=0.5, involved_decay_messages=10, involved_decay_hours=24)
    now = _time.time()
    fresh = involvement_damping(_involved_thread(0, 0), config, "U1", now)
    buried = involvement_damping(_involved_thread(0, 5), config, "U1", now)
    # More messages after my post -> less reduction -> damping closer to 1.0.
    assert fresh < buried < 1.0


def test_involvement_damping_fully_restored_when_superseded() -> None:
    config = HeatConfig(involved_damping=0.5, involved_decay_messages=10, involved_decay_hours=24)
    # messages_after >= involved_decay_messages -> msg_fade 0 -> no reduction.
    thread = _involved_thread(self_ago_s=0, messages_after=10)
    assert involvement_damping(thread, config, "U1", _time.time()) == 1.0


def test_involvement_damping_fades_over_time() -> None:
    config = HeatConfig(involved_damping=0.5, involved_decay_messages=10, involved_decay_hours=24)
    now = _time.time()
    recent = involvement_damping(_involved_thread(0, 0), config, "U1", now)
    aged = involvement_damping(_involved_thread(12 * 3600, 0), config, "U1", now)
    # 12h after my post (half the decay window) -> half the reduction.
    assert recent < aged < 1.0
    # Past the full time window -> fully restored.
    old = involvement_damping(_involved_thread(25 * 3600, 0), config, "U1", now)
    assert old == 1.0


def test_compute_heat_applies_involvement_damping() -> None:
    config = HeatConfig(involved_damping=0.5, involved_decay_messages=10, involved_decay_hours=24)
    thread = _involved_thread(self_ago_s=0, messages_after=0)
    damped = compute_heat(thread, config, "U1")
    undamped = compute_heat(thread, config, None)
    assert damped < undamped
    assert abs(damped - undamped * 0.5) < 1e-6
