import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from slack_dashboard.config import AppConfig
from slack_dashboard.heat import classify_tier, compute_heat
from slack_dashboard.slack.client import SlackClient
from slack_dashboard.slack.queue import PRIORITY_BACKFILL, FetchItem, FetchQueue
from slack_dashboard.thread import ThreadEntry

logger = logging.getLogger(__name__)


class SlackPoller:
    def __init__(
        self,
        slack_client: SlackClient,
        config: AppConfig,
        on_title_needed: Any | None = None,
        on_summary_needed: Any | None = None,
    ) -> None:
        self._slack = slack_client
        self._config = config
        self._on_title_needed = on_title_needed
        self._on_summary_needed = on_summary_needed
        self._threads: dict[tuple[str, str], ThreadEntry] = {}
        self._channel_map: dict[str, str] = {}
        self._queue = FetchQueue()
        self._consumer_task: asyncio.Task[None] | None = None

    @property
    def threads(self) -> dict[tuple[str, str], ThreadEntry]:
        return self._threads

    @property
    def queue(self) -> FetchQueue:
        return self._queue

    def ranked_threads(self) -> list[ThreadEntry]:
        threads = list(self._threads.values())
        return sorted(threads, key=lambda t: t.heat_score, reverse=True)

    async def start(self) -> None:
        self._channel_map = self._config.channels
        logger.info(
            "Loaded %d channels from config, seeding fetch queue...", len(self._channel_map)
        )
        self._queue.seed_channels(self._channel_map, priority=PRIORITY_BACKFILL)
        self._consumer_task = asyncio.create_task(self._consume_loop())

    async def stop(self) -> None:
        if self._consumer_task:
            self._consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer_task

    async def _consume_loop(self) -> None:
        try:
            while True:
                item = await self._queue.dequeue()
                await self._process_item(item)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Fatal error in fetch consumer")

    async def _process_item(self, item: FetchItem) -> None:
        try:
            if item.thread_ts is not None:
                await self._fetch_thread(item.channel_id, item.channel_name, item.thread_ts)
            else:
                await self._fetch_channel(item.channel_id, item.channel_name)
        except Exception:
            logger.exception(
                "Error processing fetch item: channel=%s thread=%s",
                item.channel_name,
                item.thread_ts,
            )

    async def _fetch_thread(self, channel_id: str, channel_name: str, thread_ts: str) -> None:
        replies = await self._slack.fetch_replies(channel_id, thread_ts)
        if not replies:
            return
        participants = {r["user"] for r in replies if "user" in r}
        last_ts = max(
            (float(r["ts"]) for r in replies),
            default=float(thread_ts),
        )
        last_activity = datetime.fromtimestamp(last_ts, tz=UTC)
        key = (channel_id, thread_ts)
        existing = self._threads.get(key)
        first_message = replies[0].get("text", "") if replies else ""
        entry = ThreadEntry(
            channel_id=channel_id,
            channel_name=channel_name,
            thread_ts=thread_ts,
            first_message=first_message,
            reply_count=len(replies) - 1,
            participants=participants,
            last_activity=last_activity,
            title=existing.title if existing else None,
            title_watermark=existing.title_watermark if existing else 0,
            summary=existing.summary if existing else None,
            summary_watermark=existing.summary_watermark if existing else 0,
        )
        self._update_heat(entry)
        self._threads[key] = entry
        reply_texts = [r.get("text", "") for r in replies if r.get("text")]
        self._maybe_trigger_llm(entry, reply_texts)

    async def _fetch_channel(self, channel_id: str, channel_name: str) -> None:
        thread_messages = await self._slack.fetch_threads(channel_id)
        for i, msg in enumerate(thread_messages):
            if i > 0:
                await asyncio.sleep(1.0)
            thread_ts = msg.get("thread_ts", msg["ts"])
            key = (channel_id, thread_ts)
            replies = await self._slack.fetch_replies(channel_id, thread_ts)
            participants = {r["user"] for r in replies if "user" in r}
            last_ts = max(
                (float(r["ts"]) for r in replies),
                default=float(thread_ts),
            )
            last_activity = datetime.fromtimestamp(last_ts, tz=UTC)
            existing = self._threads.get(key)
            entry = ThreadEntry(
                channel_id=channel_id,
                channel_name=channel_name,
                thread_ts=thread_ts,
                first_message=msg.get("text", ""),
                reply_count=len(replies) - 1,
                participants=participants,
                last_activity=last_activity,
                title=existing.title if existing else None,
                title_watermark=existing.title_watermark if existing else 0,
                summary=existing.summary if existing else None,
                summary_watermark=existing.summary_watermark if existing else 0,
            )
            self._update_heat(entry)
            self._threads[key] = entry
            reply_texts = [r.get("text", "") for r in replies if r.get("text")]
            self._maybe_trigger_llm(entry, reply_texts)

    def _update_heat(self, entry: ThreadEntry) -> None:
        entry.heat_score = compute_heat(entry, self._config.heat)
        now = datetime.now(UTC)
        minutes_inactive = (now - entry.last_activity).total_seconds() / 60
        entry.heat_tier = classify_tier(
            entry.heat_score,
            self._config.heat,
            minutes_inactive=minutes_inactive,
            cold_threshold_minutes=self._config.polling.cold_threshold_minutes,
        )

    def _maybe_trigger_llm(self, entry: ThreadEntry, reply_texts: list[str]) -> None:
        if self._on_title_needed and entry.needs_retitle(
            self._config.heat.retitle_reply_growth,
            self._config.heat.retitle_reply_percent,
        ):
            asyncio.create_task(self._on_title_needed(entry, reply_texts))
        needs_summary = entry.summary is None or entry.reply_count > entry.summary_watermark
        if self._on_summary_needed and needs_summary:
            asyncio.create_task(self._on_summary_needed(entry, reply_texts))
