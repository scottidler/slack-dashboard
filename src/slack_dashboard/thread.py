from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ThreadEntry:
    channel_id: str
    channel_name: str
    thread_ts: str
    first_message: str
    started_by: str
    reply_count: int
    # keyed by stable Slack user_id (not display name); value = message count
    participants: dict[str, int]
    last_activity: datetime
    heat_score: float = 0.0
    heat_tier: str = "cold"
    title: str | None = None
    title_watermark: int = 0
    summary: str | None = None
    summary_watermark: int = 0
    reply_timestamps: list[float] = field(default_factory=list)  # rolling window for velocity
    resurrection_event_ts: float = 0.0  # ts of reviving activity; zombie state computed from this
    first_seen_ts: float = 0.0  # thread creation time (from thread_ts), for age/resurrection
    first_observed_at: float = 0.0  # wall-clock epoch the dashboard FIRST saw this thread
    # (observation time, not thread creation time); from ObservedStore

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        return self.first_message[:80]

    def needs_retitle(self, retitle_reply_growth: int, retitle_reply_percent: int) -> bool:
        if self.title is None:
            return True
        new_replies = self.reply_count - self.title_watermark
        threshold = max(retitle_reply_growth, self.title_watermark * retitle_reply_percent / 100)
        return new_replies > threshold
