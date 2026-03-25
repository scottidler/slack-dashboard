from datetime import UTC, datetime, timedelta

from slack_dashboard.config import HeatConfig, PollingConfig, PruningConfig
from slack_dashboard.thread import ThreadEntry


def compute_heat(thread: ThreadEntry, config: HeatConfig) -> float:
    now = datetime.now(UTC)
    minutes_since = (now - thread.last_activity).total_seconds() / 60
    recency_bonus = max(0.0, config.recency_max_bonus - minutes_since)
    return (
        thread.reply_count * config.reply_weight
        + len(thread.participants) * config.participant_weight
        + recency_bonus
    )


def classify_tier(
    score: float,
    config: HeatConfig,
    minutes_inactive: float = 0,
    cold_threshold_minutes: int = 0,
) -> str:
    if cold_threshold_minutes > 0 and minutes_inactive >= cold_threshold_minutes:
        return "cold"
    if score >= config.hot_threshold:
        return "hot"
    if score >= config.warm_threshold:
        return "warm"
    return "cold"


def rank_threads(
    threads: list[ThreadEntry],
    config: HeatConfig,
    polling: PollingConfig,
) -> list[ThreadEntry]:
    now = datetime.now(UTC)
    for thread in threads:
        thread.heat_score = compute_heat(thread, config)
        minutes_inactive = (now - thread.last_activity).total_seconds() / 60
        thread.heat_tier = classify_tier(
            thread.heat_score,
            config,
            minutes_inactive=minutes_inactive,
            cold_threshold_minutes=polling.cold_threshold_minutes,
        )
    return sorted(threads, key=lambda t: t.heat_score, reverse=True)


def prune_threads(
    threads: list[ThreadEntry],
    pruning: PruningConfig,
) -> list[ThreadEntry]:
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=pruning.cold_max_hours)
    return [t for t in threads if t.last_activity > cutoff]
