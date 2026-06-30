import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from slack_dashboard.config import HeatConfig, resolve_channel_weight, resolve_person_weight
from slack_dashboard.thread import ThreadEntry

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
    base: float  # message_count*reply_weight + people_term (capped)
    message_count: int  # for the Nm face value
    people_count: int  # len(participants), for the Np face value
    people_term: float  # weighted, capped sum (tooltip / precision)
    has_vip: bool  # any participant above default weight -> append crown
    velocity: float  # raw replies/min in window
    recency: float  # decay multiplier in [decay_floor, 1.0]
    damping: float  # involvement-damping multiplier in [1-involved_damping, 1.0]


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


def involvement_damping(
    thread: ThreadEntry,
    config: HeatConfig,
    self_user_id: str | None,
    now_ts: float,
) -> float:
    """Heat multiplier that lowers priority for a thread the user recently posted in.

    Returns 1.0 (no change) when the feature is off (involved_damping <= 0), the user is
    unresolved (None), or the user has no retained post in this thread. Otherwise the
    user's last post drives a reduction strongest immediately (nothing after it, just
    posted) that fades back to 1.0 as later messages bury it and as time passes - at which
    point the thread may need the user again and regains its rank:

        msg_fade  = max(0, 1 - messages_after / involved_decay_messages)
        time_fade = max(0, 1 - hours_since    / involved_decay_hours)
        damping   = 1 - involved_damping * (msg_fade * time_fade)   # in [1-damping, 1]

    A decay knob of 0 disables that axis (treated as always-fresh on it). Logs the inputs
    and resulting multiplier per the logging rule.
    """
    if self_user_id is None or config.involved_damping <= 0:
        return 1.0
    mine = [r.ts for r in thread.replies if r.author_id == self_user_id]
    if not mine:
        return 1.0
    last_ts = max(mine)
    messages_after = sum(1 for r in thread.replies if r.ts > last_ts)
    hours_since = max(0.0, (now_ts - last_ts) / 3600)
    # A decay knob of 0 disables that axis (fade fixed at 1.0 = always fresh on it).
    decay_msgs = config.involved_decay_messages
    decay_hrs = config.involved_decay_hours
    msg_fade = max(0.0, 1.0 - messages_after / decay_msgs) if decay_msgs > 0 else 1.0
    time_fade = max(0.0, 1.0 - hours_since / decay_hrs) if decay_hrs > 0 else 1.0
    freshness = msg_fade * time_fade
    damping = 1.0 - config.involved_damping * freshness
    logger.debug(
        "involvement_damping: channel=%s thread_ts=%s messages_after=%d hours_since=%.2f "
        "msg_fade=%.3f time_fade=%.3f freshness=%.3f damping=%.3f",
        thread.channel_name,
        thread.thread_ts,
        messages_after,
        hours_since,
        msg_fade,
        time_fade,
        freshness,
        damping,
    )
    return damping


def heat_breakdown(
    thread: ThreadEntry,
    config: HeatConfig,
    self_user_id: str | None = None,
    now: float | None = None,
) -> HeatBreakdown:
    """Compute every factor behind a thread's heat score in one place.

    This is the single arithmetic path for the ranking score: ``compute_heat`` is a thin
    wrapper that returns ``.overall``. ``now`` is a float Unix timestamp defaulting to the
    current UTC instant, matching ``velocity``/``involvement_damping``/``structural_heat``.
    ``recency`` derives ``hours_since`` from the float ``now`` (the ``structural_heat``
    pattern), which assumes a tz-aware ``last_activity`` (the poller creates it aware,
    though ``ThreadEntry`` does not enforce it).
    """
    if now is None:
        now = datetime.now(UTC).timestamp()
    logger.debug(
        "heat_breakdown: channel=%s thread_ts=%s message_count=%d participants=%d "
        "self_user_id=%s now=%.6f",
        thread.channel_name,
        thread.thread_ts,
        thread.message_count,
        len(thread.participants),
        self_user_id,
        now,
    )
    hours_since = (now - thread.last_activity.timestamp()) / 3600
    # Participant term is the SUM of per-person weights (each defaults to participant_weight),
    # so important people raise a thread without erasing volume gravity. Bounded by
    # people_weight_cap so a pile-up of weighted people cannot run the score away.
    people_term = sum(resolve_person_weight(uid, config) for uid in thread.participants)
    if config.people_weight_cap > 0:
        people_term = min(people_term, config.people_weight_cap)
    message_count = thread.message_count
    people_count = len(thread.participants)
    base = (message_count * config.reply_weight) + people_term
    channel_weight = resolve_channel_weight(thread.channel_name, config)
    vel = velocity(thread, config, now)
    recency = max(config.decay_floor, 1.0 - (hours_since / config.decay_hours))
    damping = involvement_damping(thread, config, self_user_id, now)
    has_vip = is_vip(thread, config)
    score = channel_weight * (base + vel * config.velocity_weight) * recency * damping
    logger.debug(
        "heat_breakdown: channel=%s base=%.1f message_count=%d people_count=%d "
        "people_term=%.1f has_vip=%s velocity=%.3f weight=%.2f recency=%.3f "
        "damping=%.3f score=%.1f",
        thread.channel_name,
        base,
        message_count,
        people_count,
        people_term,
        has_vip,
        vel,
        channel_weight,
        recency,
        damping,
        score,
    )
    return HeatBreakdown(
        overall=score,
        channel_weight=channel_weight,
        base=base,
        message_count=message_count,
        people_count=people_count,
        people_term=people_term,
        has_vip=has_vip,
        velocity=vel,
        recency=recency,
        damping=damping,
    )


def compute_heat(thread: ThreadEntry, config: HeatConfig, self_user_id: str | None = None) -> float:
    return heat_breakdown(thread, config, self_user_id).overall


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


def is_involved(thread: ThreadEntry, self_user_id: str | None) -> bool:
    """True when the current user has personally posted in this thread.

    Membership is a plain lookup against ``participants`` (keyed by stable Slack
    user_id, includes every message author - root and replies). ``self_user_id``
    is None until auth.test resolves it (or if that fails), in which case this is
    always False so the 👤 glyph stays dark rather than misfiring.

    Trivial membership check - no logging per the logging rule.
    """
    return self_user_id is not None and self_user_id in thread.participants


def classify_tier(score: float, config: HeatConfig) -> str:
    if score >= config.hot_threshold:
        return "hot"
    if score >= config.warm_threshold:
        return "warm"
    return "cold"


def rank_threads(
    threads: list[ThreadEntry],
    config: HeatConfig,
    self_user_id: str | None = None,
) -> list[ThreadEntry]:
    for thread in threads:
        thread.heat_score = compute_heat(thread, config, self_user_id)
        thread.heat_tier = classify_tier(thread.heat_score, config)
    return sorted(threads, key=lambda t: t.heat_score, reverse=True)


def filter_stale_threads(
    threads: list[ThreadEntry],
    config: HeatConfig,
) -> list[ThreadEntry]:
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=config.max_thread_age_days)
    return [t for t in threads if t.last_activity > cutoff]
