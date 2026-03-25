from dataclasses import dataclass
from datetime import datetime


@dataclass
class ThreadEntry:
    channel_id: str
    channel_name: str
    thread_ts: str
    first_message: str
    reply_count: int
    participants: set[str]
    last_activity: datetime
    heat_score: float = 0.0
    heat_tier: str = "cold"
    title: str | None = None
    title_watermark: int = 0
    summary: str | None = None
    summary_watermark: int = 0

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
