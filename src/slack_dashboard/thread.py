from dataclasses import dataclass, field
from datetime import datetime

# Maximum number of reply records retained per thread.  Oldest are dropped past
# this cap.  Matches the prior MAX_REPLY_TIMESTAMPS cap from heat.py so the
# projection never grows larger than before.
MAX_REPLY_RECORDS = 500

# Maximum characters retained per reply text.  Bounds memory and LLM tokens:
# worst-case is MAX_REPLY_RECORDS * REPLY_TEXT_MAX per thread (~140 KB).
REPLY_TEXT_MAX = 280


@dataclass
class ReplyRecord:
    """One retained, ordered reply record - the single source of truth for
    timing, authorship, and text needed by structural heat and tone scoring.

    ``ts`` is the Slack message timestamp used as the dedupe key (normalized to
    6 decimal places, matching ``prune_timestamps``).  ``is_root`` marks the
    root message of the thread (the first entry in Slack's
    ``conversations_replies`` response).
    """

    ts: float
    author_id: str
    text: str  # truncated to REPLY_TEXT_MAX chars at ingestion time
    is_root: bool


def merge_replies(
    existing: list[ReplyRecord],
    incoming: list[ReplyRecord],
) -> list[ReplyRecord]:
    """Single capped, deduped, ordered merge path for all three ingestion routes.

    Keyed by ``f"{r.ts:.6f}"`` so sub-ulp float differences from socket vs REST
    round-trips collapse to a single record (``latest wins`` semantics within a
    ts).  Sorted ascending by ts; oldest records dropped past MAX_REPLY_RECORDS.

    Per the logging rule: never log full reply text - log counts only.
    """
    by_key: dict[str, ReplyRecord] = {f"{r.ts:.6f}": r for r in existing}
    for r in incoming:
        by_key[f"{r.ts:.6f}"] = r  # latest wins for a given ts
    merged = sorted(by_key.values(), key=lambda r: r.ts)
    result = merged[-MAX_REPLY_RECORDS:]
    return result


@dataclass
class ThreadEntry:
    channel_id: str
    channel_name: str
    thread_ts: str
    first_message: str
    started_by: str
    message_count: int
    # keyed by stable Slack user_id (not display name); value = message count
    participants: dict[str, int]
    last_activity: datetime
    heat_score: float = 0.0
    heat_tier: str = "cold"
    title: str | None = None
    title_watermark: int = 0
    summary: str | None = None
    summary_watermark: int = 0
    # Ordered, capped, deduped reply records - single source of truth for
    # timing, authorship, and text.  reply_timestamps is a derived projection.
    replies: list[ReplyRecord] = field(default_factory=list)
    resurrection_event_ts: float = 0.0  # ts of reviving activity; zombie state computed from this
    first_seen_ts: float = 0.0  # thread creation time (from thread_ts), for age/resurrection
    first_observed_at: float = 0.0  # wall-clock epoch the dashboard FIRST saw this thread
    # (observation time, not thread creation time); from ObservedStore
    # Stored tone score 0-3 from LLM summary; 0 until a summary is generated.
    heated_tone: int = 0

    @property
    def reply_timestamps(self) -> list[float]:
        """Derived projection of replies - the single source of truth.

        Velocity, prune_timestamps, and all heat computations consume this
        projection; no dual-write needed.  The list is sorted ascending by ts
        (merge_replies guarantees order).
        """
        return [r.ts for r in self.replies]

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        return self.first_message[:80]

    def needs_retitle(self, retitle_reply_growth: int, retitle_reply_percent: int) -> bool:
        if self.title is None:
            return True
        new_replies = self.message_count - self.title_watermark
        threshold = max(retitle_reply_growth, self.title_watermark * retitle_reply_percent / 100)
        return new_replies > threshold
