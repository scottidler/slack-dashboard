import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from slack_dashboard.config import AppConfig
from slack_dashboard.heat import classify_tier, compute_heat, filter_stale_threads
from slack_dashboard.slack.client import SlackClient
from slack_dashboard.slack.queue import (
    PRIORITY_BACKFILL,
    PRIORITY_REFRESH,
    FetchItem,
    FetchQueue,
)
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
        self._refresh_task: asyncio.Task[None] | None = None
        self._worker_semaphore = asyncio.Semaphore(10)
        self._active_workers: set[asyncio.Task[None]] = set()
        self._channel_watermarks: dict[str, str] = {}
        self._thread_watermarks: dict[tuple[str, str], str] = {}
        self._user_cache: dict[str, str] = {}

    @property
    def threads(self) -> dict[tuple[str, str], ThreadEntry]:
        return self._threads

    @property
    def queue(self) -> FetchQueue:
        return self._queue

    @property
    def channel_watermarks(self) -> dict[str, str]:
        return self._channel_watermarks

    @property
    def thread_watermarks(self) -> dict[tuple[str, str], str]:
        return self._thread_watermarks

    def ranked_threads(self) -> list[ThreadEntry]:
        threads = filter_stale_threads(list(self._threads.values()), self._config.heat)
        for t in threads:
            t.heat_score = compute_heat(t, self._config.heat)
            t.heat_tier = classify_tier(t.heat_score, self._config.heat)
        return sorted(threads, key=lambda t: t.heat_score, reverse=True)

    async def start(self) -> None:
        self._channel_map = self._config.channels
        logger.info(
            "Loaded %d channels from config, seeding fetch queue...", len(self._channel_map)
        )
        self._queue.seed_channels(self._channel_map, priority=PRIORITY_BACKFILL)
        self._consumer_task = asyncio.create_task(self._consume_loop())
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        for task in [self._consumer_task, self._refresh_task]:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _consume_loop(self) -> None:
        try:
            while True:
                item = await self._queue.dequeue()
                await self._worker_semaphore.acquire()
                task = asyncio.create_task(self._run_worker(item))
                self._active_workers.add(task)
                task.add_done_callback(self._active_workers.discard)
        except asyncio.CancelledError:
            for task in self._active_workers:
                task.cancel()
            return
        except Exception:
            logger.exception("Fatal error in fetch consumer")

    async def _run_worker(self, item: FetchItem) -> None:
        try:
            await self._process_item(item)
        finally:
            self._worker_semaphore.release()

    async def _refresh_loop(self) -> None:
        interval = self._config.fetch.refresh_interval_minutes * 60
        try:
            while True:
                await asyncio.sleep(interval)
                logger.info("Periodic refresh: re-queuing all channels")
                self._queue.seed_channels(self._channel_map, priority=PRIORITY_REFRESH)
        except asyncio.CancelledError:
            return

    async def _process_item(self, item: FetchItem) -> None:
        try:
            incremental = item.priority == PRIORITY_REFRESH
            if item.thread_ts is not None:
                await self._fetch_thread(
                    item.channel_id, item.channel_name, item.thread_ts, incremental=incremental
                )
            else:
                await self._fetch_channel(
                    item.channel_id, item.channel_name, incremental=incremental
                )
        except Exception:
            logger.exception(
                "Error processing fetch item: channel=%s thread=%s",
                item.channel_name,
                item.thread_ts,
            )

    async def _fetch_thread(
        self,
        channel_id: str,
        channel_name: str,
        thread_ts: str,
        *,
        incremental: bool = False,
    ) -> None:
        key = (channel_id, thread_ts)
        oldest = self._thread_watermarks.get(key) if incremental else None
        replies = await self._slack.fetch_replies(channel_id, thread_ts, oldest=oldest)
        if not replies:
            return

        latest_ts = max(r["ts"] for r in replies)
        self._thread_watermarks[key] = latest_ts

        existing = self._threads.get(key)
        if incremental and existing and oldest:
            new_replies = [r for r in replies if r["ts"] != thread_ts]
            for r in new_replies:
                if "user" in r:
                    name = await self._resolve_user(r["user"])
                    existing.participants[name] = existing.participants.get(name, 0) + 1
            existing.reply_count += len(new_replies)
            last_activity = datetime.fromtimestamp(float(latest_ts), tz=UTC)
            if last_activity > existing.last_activity:
                existing.last_activity = last_activity
            self._update_heat(existing)
            reply_texts = [r.get("text", "") for r in new_replies if r.get("text")]
            if reply_texts:
                self._maybe_trigger_llm(existing, reply_texts)
            return

        participants: dict[str, int] = {}
        for r in replies:
            if "user" in r:
                name = await self._resolve_user(r["user"])
                participants[name] = participants.get(name, 0) + 1
        last_activity = datetime.fromtimestamp(float(latest_ts), tz=UTC)
        first_message = replies[0].get("text", "") if replies else ""
        started_by_id = replies[0].get("user", "") if replies else ""
        started_by_name = (
            existing.started_by
            if existing and existing.started_by
            else await self._resolve_user(started_by_id)
        )
        entry = ThreadEntry(
            channel_id=channel_id,
            channel_name=channel_name,
            thread_ts=thread_ts,
            first_message=first_message,
            started_by=started_by_name,
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

    async def _fetch_channel(
        self,
        channel_id: str,
        channel_name: str,
        *,
        incremental: bool = False,
    ) -> None:
        oldest = self._channel_watermarks.get(channel_id) if incremental else None
        thread_messages = await self._slack.fetch_threads(
            channel_id, min_replies=self._config.fetch.min_replies, oldest=oldest
        )

        if thread_messages:
            latest_ts = max(msg.get("ts", "0") for msg in thread_messages)
            existing_wm = self._channel_watermarks.get(channel_id, "0")
            if latest_ts > existing_wm:
                self._channel_watermarks[channel_id] = latest_ts

        for msg in thread_messages:
            thread_ts = msg.get("thread_ts", msg["ts"])
            await self._fetch_thread(channel_id, channel_name, thread_ts, incremental=incremental)

    async def _resolve_user(self, user_id: str) -> str:
        if not user_id:
            return ""
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        name = await self._slack.resolve_user(user_id)
        self._user_cache[user_id] = name
        return name

    def _update_heat(self, entry: ThreadEntry) -> None:
        entry.heat_score = compute_heat(entry, self._config.heat)
        entry.heat_tier = classify_tier(entry.heat_score, self._config.heat)

    def _maybe_trigger_llm(self, entry: ThreadEntry, reply_texts: list[str]) -> None:
        if self._on_title_needed and entry.needs_retitle(
            self._config.heat.retitle_reply_growth,
            self._config.heat.retitle_reply_percent,
        ):
            asyncio.create_task(self._on_title_needed(entry, reply_texts))
        needs_summary = entry.summary is None or entry.reply_count > entry.summary_watermark
        if self._on_summary_needed and needs_summary:
            asyncio.create_task(self._on_summary_needed(entry, reply_texts))
