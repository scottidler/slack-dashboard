import logging
from datetime import UTC, datetime, timedelta

from slack_dashboard.config import HeatConfig, resolve_channel_weight, resolve_person_weight
from slack_dashboard.thread import ThreadEntry

logger = logging.getLogger(__name__)

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


def compute_heat(thread: ThreadEntry, config: HeatConfig) -> float:
    now = datetime.now(UTC)
    hours_since = (now - thread.last_activity).total_seconds() / 3600
    # Participant term is the SUM of per-person weights (each defaults to participant_weight),
    # so important people raise a thread without erasing volume gravity. Bounded by
    # people_weight_cap so a pile-up of weighted people cannot run the score away.
    people_term = sum(resolve_person_weight(uid, config) for uid in thread.participants)
    if config.people_weight_cap > 0:
        people_term = min(people_term, config.people_weight_cap)
    base = (thread.message_count * config.reply_weight) + people_term
    channel_weight = resolve_channel_weight(thread.channel_name, config)
    vel = velocity(thread, config, now.timestamp())
    recency = max(config.decay_floor, 1.0 - (hours_since / config.decay_hours))
    score = channel_weight * (base + vel * config.velocity_weight) * recency
    logger.debug(
        "compute_heat: channel=%s base=%.1f velocity=%.3f weight=%.2f recency=%.3f score=%.1f",
        thread.channel_name,
        base,
        vel,
        channel_weight,
        recency,
        score,
    )
    return score


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

    In Phase 1 tone_term = 0 (heated_tone is 0 until Phase 2 LLM changes land).
    heated_score = structural_term + tone_term; fires when >= heated_threshold.

    Logs heated_score, structural term, tone term, threshold, and the fire decision
    per the logging rule.
    """
    if now_ts is None:
        now_ts = datetime.now(UTC).timestamp()

    s_term = structural_heat(thread, config, now_ts)
    tone_term = thread.heated_tone * config.heated_tone_weight  # 0 in Phase 1
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
        "FIRE" if fired else "no",
    )
    return fired


def classify_tier(score: float, config: HeatConfig) -> str:
    if score >= config.hot_threshold:
        return "hot"
    if score >= config.warm_threshold:
        return "warm"
    return "cold"


def rank_threads(
    threads: list[ThreadEntry],
    config: HeatConfig,
) -> list[ThreadEntry]:
    for thread in threads:
        thread.heat_score = compute_heat(thread, config)
        thread.heat_tier = classify_tier(thread.heat_score, config)
    return sorted(threads, key=lambda t: t.heat_score, reverse=True)


def filter_stale_threads(
    threads: list[ThreadEntry],
    config: HeatConfig,
) -> list[ThreadEntry]:
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=config.max_thread_age_days)
    return [t for t in threads if t.last_activity > cutoff]
