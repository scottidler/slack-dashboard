import logging
from datetime import UTC, datetime, timedelta

from slack_dashboard.config import HeatConfig, resolve_channel_weight
from slack_dashboard.thread import ThreadEntry

logger = logging.getLogger(__name__)

# Hard cap on retained reply timestamps per thread so a high-velocity thread
# cannot grow the velocity window unbounded (oldest dropped past this cap).
MAX_REPLY_TIMESTAMPS = 500


def prune_timestamps(
    timestamps: list[float], config: HeatConfig, now_ts: float | None = None
) -> list[float]:
    """Drop timestamps outside the velocity window and cap the retained count.

    Returns a new, sorted list of the most recent in-window timestamps.
    """
    if now_ts is None:
        now_ts = datetime.now(UTC).timestamp()
    cutoff = now_ts - config.velocity_window_minutes * 60
    in_window = sorted(ts for ts in timestamps if ts >= cutoff)
    if len(in_window) > MAX_REPLY_TIMESTAMPS:
        in_window = in_window[-MAX_REPLY_TIMESTAMPS:]
    return in_window


def velocity(thread: ThreadEntry, config: HeatConfig, now_ts: float | None = None) -> float:
    """Replies within the velocity window per minute."""
    if config.velocity_window_minutes <= 0:
        return 0.0
    if now_ts is None:
        now_ts = datetime.now(UTC).timestamp()
    cutoff = now_ts - config.velocity_window_minutes * 60
    recent = sum(1 for ts in thread.reply_timestamps if ts >= cutoff)
    return recent / config.velocity_window_minutes


def compute_heat(thread: ThreadEntry, config: HeatConfig) -> float:
    now = datetime.now(UTC)
    hours_since = (now - thread.last_activity).total_seconds() / 3600
    base = (thread.reply_count * config.reply_weight) + (
        len(thread.participants) * config.participant_weight
    )
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
