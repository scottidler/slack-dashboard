from datetime import UTC, datetime, timedelta

from slack_dashboard.config import HeatConfig
from slack_dashboard.thread import ThreadEntry


def compute_heat(thread: ThreadEntry, config: HeatConfig) -> float:
    now = datetime.now(UTC)
    hours_since = (now - thread.last_activity).total_seconds() / 3600
    base = (thread.reply_count * config.reply_weight) + (
        len(thread.participants) * config.participant_weight
    )
    decay = max(0.01, 1.0 - (hours_since / config.decay_half_life_hours))
    return base * decay


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
