import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from slack_dashboard.config import HeatConfig, resolve_channel_weight, resolve_person_weight
from slack_dashboard.thread import ThreadEntry
from slack_dashboard.worktime import business_hours_between

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HeatBreakdown:
    """The composed factors behind a single heat score, exposed in one struct.

    Every field is a quantity that enters ``compute_heat``'s formula or names a count a
    human scans. ``overall`` is the ranking score and equals ``compute_heat``'s result for
    the same inputs; the other fields are the intermediates that produce it. This is the
    single arithmetic path - ``compute_heat`` returns ``heat_breakdown(...).overall``.
    """

    overall: float  # the ranking score (== compute_heat result)
    channel_weight: float  # multiplier
    base: float  # base_norm: hard-ceilinged saturation of volume -> [0, base_cap)
    message_count: int  # for the Nm face value
    people_count: int  # len(participants), for the Np face value
    people_term: float  # weighted, capped sum (tooltip / precision)
    has_vip: bool  # any participant above default weight -> append crown
    velocity: float  # raw replies/min in window
    atrophy: float  # working-hours exponential decay multiplier in (0, 1]
    activity: float  # freshness burst = min(activity_cap, velocity*velocity_weight)
    alive_boost: float  # longevity lift, freshness-gated: 1 + alive_weight*f(time_alive)*atrophy
    damping: float  # following drop-and-rebuild multiplier in [followed_drop, 1.0]
    time_alive: float  # working hours, first_post -> last_post
    time_since_last: float  # working hours, last_post -> now (the atrophy input)
    # Topic terms (jev). votes is additive beside base and activity; gates multiplies the
    # whole score. Both are their neutral value (0, 1) when the thread is unrated or every
    # topic knob is off. p_leader / p_resolved are None until jev has rated the thread.
    votes: float = 0.0
    gates: float = 1.0
    p_leader: float | None = None
    p_resolved: float | None = None


def is_vip(thread: ThreadEntry, config: HeatConfig) -> bool:
    """True when any participant carries an above-default people-weight (a pinned person).

    The single source of truth for the VIP rule: a participant is a VIP when their
    ``resolve_person_weight`` exceeds the default ``participant_weight``. Both the heat
    breakdown's crown and ``web._has_vip`` delegate here so the rule lives in one place.

    Trivial membership predicate - no logging per the logging rule.
    """
    default = float(config.participant_weight)
    return any(resolve_person_weight(uid, config) > default for uid in thread.participants)


# Hard cap on retained reply timestamps per thread so a high-velocity thread
# cannot grow the velocity window unbounded (oldest dropped past this cap).
# Kept for backward compat; the actual cap is now MAX_REPLY_RECORDS in thread.py
# (replies list) and this constant governs prune_timestamps on the projection.
MAX_REPLY_TIMESTAMPS = 500


def prune_timestamps(
    timestamps: list[float], config: HeatConfig, now_ts: float | None = None
) -> list[float]:
    """Drop timestamps outside the velocity window, deduplicate, and cap the count.

    Dedup is by a normalized Slack-ts key (microsecond precision), not exact-float
    identity: the socket and REST paths can record the same reply with a sub-ulp
    difference (the listener used to round-trip through datetime), so an exact-float
    set would not collapse them and velocity would double-count. Returns a new,
    sorted list of the most recent in-window timestamps.
    """
    if now_ts is None:
        now_ts = datetime.now(UTC).timestamp()
    cutoff = now_ts - config.velocity_window_minutes * 60
    deduped: dict[str, float] = {}
    for ts in timestamps:
        if ts >= cutoff:
            deduped[f"{ts:.6f}"] = ts
    in_window = sorted(deduped.values())
    if len(in_window) > MAX_REPLY_TIMESTAMPS:
        in_window = in_window[-MAX_REPLY_TIMESTAMPS:]
    return in_window


def replies_in_window(thread: ThreadEntry, config: HeatConfig, now_ts: float | None = None) -> int:
    """Count of replies within the velocity window (the raw count, not per-minute)."""
    if config.velocity_window_minutes <= 0:
        return 0
    if now_ts is None:
        now_ts = datetime.now(UTC).timestamp()
    cutoff = now_ts - config.velocity_window_minutes * 60
    return sum(1 for ts in thread.reply_timestamps if ts >= cutoff)


def velocity(thread: ThreadEntry, config: HeatConfig, now_ts: float | None = None) -> float:
    """Replies within the velocity window per minute."""
    if config.velocity_window_minutes <= 0:
        return 0.0
    return replies_in_window(thread, config, now_ts) / config.velocity_window_minutes


def following_damping(thread: ThreadEntry, config: HeatConfig, now_ts: float) -> float:
    """Drop-and-rebuild heat multiplier for a thread Slack follows for me.

    Returns 1.0 (no change) when the thread is not followed or the feature is off
    (``followed_drop >= 1.0``). A followed thread drops to ``followed_drop``; each retained
    non-root reply after my Slack ``last_read`` (unread in Slack) rebuilds it toward 1.0 at
    rate ``followed_rebuild_per_msg``:

        damping = followed_drop + (1 - followed_drop) * (unread * followed_rebuild_per_msg)

    An unknown ``last_read`` (None) counts no unread replies: unknown read state is not
    "unread everything". Clamped to ``[followed_drop, 1.0]``.
    """
    if not is_following(thread) or config.followed_drop >= 1.0:
        return 1.0
    last_read = thread.last_read
    unread = (
        0
        if last_read is None
        else sum(1 for r in thread.replies if not r.is_root and r.ts > last_read)
    )
    rebuild = min(1.0, max(0.0, unread * config.followed_rebuild_per_msg))
    damping = config.followed_drop + (1.0 - config.followed_drop) * rebuild
    damping = min(1.0, max(config.followed_drop, damping))
    logger.debug(
        "following_damping: channel=%s thread_ts=%s last_read=%s unread=%d "
        "followed_drop=%.3f rebuild=%.3f damping=%.3f now=%.3f",
        thread.channel_name,
        thread.thread_ts,
        last_read,
        unread,
        config.followed_drop,
        rebuild,
        damping,
        now_ts,
    )
    return damping


def heat_breakdown(
    thread: ThreadEntry,
    config: HeatConfig,
    now: float | None = None,
) -> HeatBreakdown:
    """Compute every factor behind a thread's heat score in one place.

    This is the single arithmetic path for the ranking score: ``compute_heat`` is a thin
    wrapper that returns ``.overall``. ``now`` is a float Unix timestamp defaulting to the
    current UTC instant, matching ``velocity``/``following_damping``/``structural_heat``.

    The re-shaped score (one line):

        score       = channel_weight * (base_norm + activity + votes)
                      * atrophy * alive_boost * damping * gates
        volume      = message_count*reply_weight + people_term(capped)
        base_norm   = base_cap * volume / (volume + base_k)          # HARD-ceilinged, monotone
        activity    = min(activity_cap, velocity * velocity_weight)  # burst, OUTSIDE the ceiling
        votes       = leader_weight * p(leader)                      # a leader thread is full-size
        half        = half_life * (1 + atrophy_leader_stretch * (p(leader) - 0.5))
        atrophy     = 0.5 ** (time_since_last / half)                # or 1 / (1 + t / half)
        alive_boost = 1 + alive_weight * f(time_alive) * atrophy     # freshness-gated lift
        damping     = following_damping(...) in [followed_drop, 1]
        gates       = (floor + (1 - floor) * p(leader)) * (1 - resolved_drop * p(resolved))

    The topic terms read ``thread.topics`` (jev probabilities, see ``topics.py``). An unrated
    thread (``topics is None``) scores with votes 0, gates 1 and the plain half-life: the
    terms are neutral, never a penalty for not having been rated yet. With every topic knob
    at its default the formula is exactly the pre-topic score.

    ``time_since_last`` (last-post -> now) and ``time_alive`` (first-post -> last-post) are
    both fractional WORKING hours via ``business_hours_between``: nights and weekends
    contribute zero, so a Friday-afternoon thread does not go stone-cold over the weekend.
    """
    if now is None:
        now = datetime.now(UTC).timestamp()
    logger.debug(
        "heat_breakdown: channel=%s thread_ts=%s message_count=%d participants=%d "
        "subscribed=%s now=%.6f",
        thread.channel_name,
        thread.thread_ts,
        thread.message_count,
        len(thread.participants),
        thread.subscribed,
        now,
    )
    # Participant term is the SUM of per-person weights (each defaults to participant_weight),
    # so important people raise a thread without erasing volume gravity. Bounded by
    # people_weight_cap so a pile-up of weighted people cannot run the score away.
    people_term = sum(resolve_person_weight(uid, config) for uid in thread.participants)
    if config.people_weight_cap > 0:
        people_term = min(people_term, config.people_weight_cap)
    message_count = thread.message_count
    people_count = len(thread.participants)
    volume = (message_count * config.reply_weight) + people_term
    # HARD asymptotic ceiling: base_norm -> base_cap as volume -> inf, monotone. A huge
    # stale thread and a modest one both approach base_cap, so volume can no longer dominate.
    base_norm = config.base_cap * volume / (volume + config.base_k) if volume > 0 else 0.0

    channel_weight = resolve_channel_weight(thread.channel_name, config)
    vel = velocity(thread, config, now)
    # Velocity kept OUTSIDE the volume ceiling as its own bounded additive burst term.
    activity = min(config.activity_cap, vel * config.velocity_weight)

    # time_since_last: last-post -> now (the atrophy input). last_activity is the last-post
    # instant; business_hours_between clamps < 0 to 0 (after-hours post -> atrophy 1.0).
    time_since_last = business_hours_between(
        thread.last_activity.timestamp(), now, config.work_window
    )
    # Topic probabilities: None until jev rates the thread, and every term below treats
    # None as its neutral value (half-life unchanged, votes 0, gates 1).
    p_leader = thread.topics.get("leader") if thread.topics is not None else None
    p_resolved = thread.topics.get("resolved") if thread.topics is not None else None
    half = config.atrophy_half_life_work_hours
    if p_leader is not None:
        half *= 1.0 + config.atrophy_leader_stretch * (p_leader - 0.5)
    ratio = time_since_last / half
    atrophy = 1.0 / (1.0 + ratio) if config.atrophy_curve == "hyperbolic" else 0.5**ratio

    # time_alive: first-post -> last-post. first_seen_ts is the thread root epoch; fall back
    # to float(thread_ts) when unset. A monologue has time_alive == 0 (alive_boost no-op).
    first_post_ts = thread.first_seen_ts if thread.first_seen_ts > 0 else float(thread.thread_ts)
    time_alive = business_hours_between(
        first_post_ts, thread.last_activity.timestamp(), config.work_window
    )
    # f(time_alive) in [0, 1); alive_boost lifts a long-lived thread ONLY while it is fresh
    # (x atrophy gate), so an idle long-lived thread collapses back toward 1.0 as atrophy -> 0.
    alive_f = time_alive / (time_alive + config.alive_k) if time_alive > 0 else 0.0
    alive_boost = 1.0 + config.alive_weight * alive_f * atrophy

    damping = following_damping(thread, config, now)
    has_vip = is_vip(thread, config)
    votes = config.leader_weight * p_leader if p_leader is not None else 0.0
    gates = 1.0
    if p_leader is not None:
        gates *= config.leader_gate_floor + (1.0 - config.leader_gate_floor) * p_leader
    if p_resolved is not None:
        gates *= 1.0 - config.resolved_drop * p_resolved
    score = (
        channel_weight * (base_norm + activity + votes) * atrophy * alive_boost * damping * gates
    )
    logger.debug(
        "heat_breakdown: channel=%s volume=%.1f base_norm=%.3f message_count=%d "
        "people_count=%d people_term=%.1f has_vip=%s velocity=%.3f activity=%.3f "
        "weight=%.2f atrophy=%.3f alive_boost=%.3f time_alive=%.2f time_since_last=%.2f "
        "damping=%.3f votes=%.2f gates=%.3f p_leader=%s p_resolved=%s tier_method=%s "
        "score=%.3f",
        thread.channel_name,
        volume,
        base_norm,
        message_count,
        people_count,
        people_term,
        has_vip,
        vel,
        activity,
        channel_weight,
        atrophy,
        alive_boost,
        time_alive,
        time_since_last,
        damping,
        votes,
        gates,
        p_leader,
        p_resolved,
        config.tier_method,
        score,
    )
    return HeatBreakdown(
        overall=score,
        channel_weight=channel_weight,
        base=base_norm,
        message_count=message_count,
        people_count=people_count,
        people_term=people_term,
        has_vip=has_vip,
        velocity=vel,
        atrophy=atrophy,
        activity=activity,
        alive_boost=alive_boost,
        damping=damping,
        time_alive=time_alive,
        time_since_last=time_since_last,
        votes=votes,
        gates=gates,
        p_leader=p_leader,
        p_resolved=p_resolved,
    )


def compute_heat(thread: ThreadEntry, config: HeatConfig) -> float:
    return heat_breakdown(thread, config).overall


def detect_resurrection(prior_last_activity_ts: float, event_ts: float, config: HeatConfig) -> bool:
    """True when fresh activity lands after a quiet gap exceeding the threshold.

    The caller must read the prior last_activity *before* overwriting it; once a
    write path bumps last_activity, the gap is gone and resurrection can never trip.
    """
    if prior_last_activity_ts <= 0:
        return False
    gap_hours = (event_ts - prior_last_activity_ts) / 3600
    resurrected = gap_hours >= config.resurrection_gap_hours
    if resurrected:
        logger.debug(
            "detect_resurrection: gap=%.1fh threshold=%dh -> resurrected",
            gap_hours,
            config.resurrection_gap_hours,
        )
    return resurrected


def reconstruct_resurrection(sorted_reply_ts: list[float], config: HeatConfig) -> float:
    """Derive the resurrection event from a thread's full reply timeline.

    State-independent: given all reply timestamps (sorted ascending), find the most
    recent adjacent gap that exceeds resurrection_gap_hours and return the timestamp
    of the reply that *ended* that gap (i.e. the reviving activity). Returns 0.0 when
    no such gap exists. This replaces in-memory carry-forward in the full-fetch path so
    resurrection survives eviction and restart; is_zombie still gates display on age +
    recency, so this does not need to re-check thread age.
    """
    gap_seconds = config.resurrection_gap_hours * 3600
    event_ts = 0.0
    for prev, cur in zip(sorted_reply_ts, sorted_reply_ts[1:], strict=False):
        if cur - prev >= gap_seconds:
            event_ts = cur  # keep the latest qualifying gap (iteration is ascending)
    if event_ts:
        logger.debug("reconstruct_resurrection: event_ts=%.6f from reply gaps", event_ts)
    return event_ts


def is_zombie(thread: ThreadEntry, config: HeatConfig, now_ts: float | None = None) -> bool:
    """Zombie state, computed at rank time (never a sticky flag).

    Shows while the reviving activity is recent (within resurrection_display_hours)
    and the thread itself is old (first_seen older than resurrection_age_days).
    """
    if thread.resurrection_event_ts <= 0 or thread.first_seen_ts <= 0:
        return False
    if now_ts is None:
        now_ts = datetime.now(UTC).timestamp()
    if now_ts - thread.resurrection_event_ts >= config.resurrection_display_hours * 3600:
        return False
    age_days = (now_ts - thread.first_seen_ts) / 86400
    return age_days > config.resurrection_age_days


def structural_heat(thread: ThreadEntry, config: HeatConfig, now_ts: float | None = None) -> float:
    """Structural heated-exchange term, computed render-time from the replies record.

    Measures the *shape* of a fight: a real back-and-forth (alternating authors) between
    few people, fired off fast, AND recent.  Deterministic - no LLM.

    Math (pass-2, both-reviewer-convergent):
      exchange  = alternations / max(1, n-1)            # 0..1: real back-and-forth
      volume    = message_count + replies_in_window      # raw activity
      intensity = exchange * volume                      # gated by exchange, not raw velocity
      capped    = min(10, intensity * scale)             # clamp FIRST -> 0..10
      decay     = max(0, 1 - hours_since_last/decay_h)  # NO floor -> reaches 0 with age
      result    = capped * decay                         # 0..10, decays to 0

    A fast monologue has exchange~=0 -> intensity~=0 -> 0.  An old fight decays to 0
    (no floor, unlike compute_heat which has decay_floor for ranking stability).

    Per the logging rule: logs heated_score, structural term, tone term, threshold,
    and the fire decision.
    """
    if now_ts is None:
        now_ts = datetime.now(UTC).timestamp()

    authors = [r.author_id for r in thread.replies]
    n = len(authors)

    # A monologue (one author or no replies) is never a heated exchange.
    distinct = set(authors)
    if len(distinct) < 2:
        logger.debug(
            "structural_heat: channel=%s thread_ts=%s monologue (distinct=%d) -> 0",
            thread.channel_name,
            thread.thread_ts,
            len(distinct),
        )
        return 0.0

    alternations = sum(1 for i in range(1, n) if authors[i] != authors[i - 1])
    exchange = alternations / max(1, n - 1)  # 0..1

    vol = thread.message_count + replies_in_window(thread, config, now_ts)
    intensity = exchange * vol  # gated by exchange -> not raw velocity
    capped = min(10.0, intensity * config.heated_structural_scale)  # clamp FIRST

    hours_since_last = (now_ts - thread.last_activity.timestamp()) / 3600
    decay = max(0.0, 1.0 - hours_since_last / config.decay_hours)  # NO floor -> reaches 0

    result = capped * decay
    logger.debug(
        "structural_heat: channel=%s thread_ts=%s n=%d distinct=%d "
        "alternations=%d exchange=%.3f vol=%.1f intensity=%.3f capped=%.3f "
        "hours_since=%.2f decay=%.3f structural=%.3f",
        thread.channel_name,
        thread.thread_ts,
        n,
        len(distinct),
        alternations,
        exchange,
        vol,
        intensity,
        capped,
        hours_since_last,
        decay,
        result,
    )
    return result


def is_heated(thread: ThreadEntry, config: HeatConfig, now_ts: float | None = None) -> bool:
    """Heated-exchange state, computed render-time (like is_zombie).

    heated_score = structural_term + tone_term; fires when >= heated_threshold.
    tone_term = heated_tone (0-3, stored from the LLM summary) * heated_tone_weight,
    so a strong tone alone (3 * 3.0 = 9 >= 8) clears the threshold even on a
    low-volume thread, while a thread with no summary yet still fires on structure.

    Logs heated_score, structural term, tone term, threshold, and the fire decision
    per the logging rule.
    """
    if now_ts is None:
        now_ts = datetime.now(UTC).timestamp()

    s_term = structural_heat(thread, config, now_ts)
    tone_term = thread.heated_tone * config.heated_tone_weight  # 0..3 * weight
    heated_score = s_term + tone_term

    fired = heated_score >= config.heated_threshold
    logger.debug(
        "is_heated: channel=%s thread_ts=%s heated_score=%.3f structural=%.3f "
        "tone=%.3f (heated_tone=%d * weight=%.2f) threshold=%.2f -> %s",
        thread.channel_name,
        thread.thread_ts,
        heated_score,
        s_term,
        tone_term,
        thread.heated_tone,
        config.heated_tone_weight,
        config.heated_threshold,
        "HEATED" if fired else "no",
    )
    return fired


def is_following(thread: ThreadEntry) -> bool:
    """True when Slack follows this thread for me (Slack's ``subscribed`` on the root).

    Slack's definition: I started it, commented in it, was @'d in it, or followed it by
    hand. Trivial field read - no logging per the logging rule.
    """
    return thread.subscribed


def classify_tier(score: float, rank: int, total: int, config: HeatConfig) -> str:
    """Assign a heat tier, selected by ``tier-method``. Must be called POST-SORT.

    ``rank`` is the thread's zero-based index in the final descending-by-score order and
    ``total`` is the board size; both are needed by relative mode. The absolute mode
    ignores them. This is the only tiering path - neither call site re-implements it.

    - **absolute**: ``score >= tier_hot`` -> hot, ``>= tier_warm`` -> warm, else cold.
    - **relative** (hybrid): hot when ``rank < tier_hot_count AND score >= tier_floor``;
      warm when ``rank < tier_warm_count AND score >= tier_floor``; else cold. The
      absolute ``tier_floor`` makes ``stale_is_cold`` hold even in relative mode - on a
      fully-atrophied board, top-N still yields zero hot because nothing clears the floor.
      Counts clamp to ``min(count, total)`` so a small board never errors.
    """
    if config.tier_method == "relative":
        hot_count = min(config.tier_hot_count, total)
        warm_count = min(config.tier_warm_count, total)
        if rank < hot_count and score >= config.tier_floor:
            return "hot"
        if rank < warm_count and score >= config.tier_floor:
            return "warm"
        return "cold"
    # absolute (default)
    if score >= config.tier_hot:
        return "hot"
    if score >= config.tier_warm:
        return "warm"
    return "cold"


@dataclass(frozen=True)
class Ranked:
    """One thread's place on a board scored under a given config: the entry, its score, its tier.

    The score and tier belong to the config that produced them, not to the entry, so a board
    scored under a viewer's overrides never touches the shared ``ThreadEntry``.
    """

    entry: ThreadEntry
    score: float
    tier: str


def score_threads(threads: list[ThreadEntry], config: HeatConfig, now: float) -> list[Ranked]:
    """Score, sort descending and tier ``threads`` under ``config`` at ``now``. Pure.

    The one scoring path: the poller's yml ranking and a request's overridden ranking both
    come through here. Nothing is written into the entries; ``rank_threads`` and
    ``SlackPoller.ranked_threads`` write the result back for the poller's own uses.
    """
    scored = sorted(
        ((t, heat_breakdown(t, config, now).overall) for t in threads),
        key=lambda pair: pair[1],
        reverse=True,
    )
    total = len(scored)
    # Tiers are classified over the sorted list with the post-sort rank (relative mode).
    ranked = [
        Ranked(entry, score, classify_tier(score, rank, total, config))
        for rank, (entry, score) in enumerate(scored)
    ]
    logger.debug(
        "score_threads: count=%d now=%.3f tier_method=%s top=%s",
        total,
        now,
        config.tier_method,
        [(r.entry.thread_ts, round(r.score, 3), r.tier) for r in ranked[:3]],
    )
    return ranked


def write_back(ranked: list[Ranked]) -> list[ThreadEntry]:
    """Store each ranking's score and tier on its entry; return the entries in rank order."""
    for r in ranked:
        r.entry.heat_score = r.score
        r.entry.heat_tier = r.tier
    return [r.entry for r in ranked]


def rank_threads(
    threads: list[ThreadEntry],
    config: HeatConfig,
    now: float | None = None,
) -> list[ThreadEntry]:
    """Rank and write the score and tier back into the entries (the mutating wrapper)."""
    if now is None:
        now = datetime.now(UTC).timestamp()
    return write_back(score_threads(threads, config, now))


def filter_stale_threads(
    threads: list[ThreadEntry],
    config: HeatConfig,
    now: float | None = None,
) -> list[ThreadEntry]:
    if now is None:
        now = datetime.now(UTC).timestamp()
    cutoff = datetime.fromtimestamp(now, UTC) - timedelta(days=config.max_thread_age_days)
    return [t for t in threads if t.last_activity > cutoff]
