import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from slack_dashboard.config import AppConfig, resolve_min_replies
from slack_dashboard.dismiss import DismissStore
from slack_dashboard.heat import (
    classify_tier,
    compute_heat,
    detect_resurrection,
    filter_stale_threads,
    is_zombie,
    reconstruct_resurrection,
)
from slack_dashboard.observed import ObservedStore
from slack_dashboard.slack.client import SlackClient
from slack_dashboard.slack.queue import (
    PRIORITY_BACKFILL,
    PRIORITY_REFRESH,
    FetchItem,
    FetchQueue,
)
from slack_dashboard.thread import REPLY_TEXT_MAX, ReplyRecord, ThreadEntry, merge_replies

logger = logging.getLogger(__name__)


class SlackPoller:
    def __init__(
        self,
        slack_client: SlackClient,
        config: AppConfig,
        on_title_needed: Any | None = None,
        on_summary_needed: Any | None = None,
        dismiss: DismissStore | None = None,
        observed: ObservedStore | None = None,
    ) -> None:
        self._slack = slack_client
        self._config = config
        self._on_title_needed = on_title_needed
        self._on_summary_needed = on_summary_needed
        self._dismiss = dismiss
        self._observed = observed
        self._threads: dict[tuple[str, str], ThreadEntry] = {}
        self._channel_map: dict[str, str] = {}
        self._queue = FetchQueue()
        self._consumer_task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._worker_semaphore = asyncio.Semaphore(10)
        self._active_workers: set[asyncio.Task[None]] = set()
        self._channel_watermarks: dict[str, str] = {}
        self._thread_watermarks: dict[tuple[str, str], str] = {}
        # Captured once in start() so the render path can suppress the new glyph
        # for the first new_window after poller start (app-start storm suppressor, M2).
        self._app_start_at: float = 0.0
        # The authenticated user's own Slack user_id, resolved once in start() via
        # auth.test. Drives the 👤 involved glyph (a thread the user has posted in).
        # None until resolved (or if auth.test fails), in which case the glyph never fires.
        self._self_user_id: str | None = None

    @property
    def self_user_id(self) -> str | None:
        """The authenticated user's Slack user_id, or None if unresolved.

        Resolved once in ``start()``; the render path uses it to mark threads the
        user has personally posted in. None (auth.test failed/not yet run) means
        the involved glyph stays dark.
        """
        return self._self_user_id

    @property
    def app_start_at(self) -> float:
        """Wall-clock epoch captured once when the poller starts.

        The render path uses this to suppress the new glyph for all threads
        within ``new_window_minutes`` of start (app-start storm suppressor, M2).
        Zero until ``start()`` is called.
        """
        return self._app_start_at

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
        live = [
            t
            for t in self._threads.values()
            if self._dismiss is None or not self._dismiss.is_dismissed(t.channel_id, t.thread_ts)
        ]
        threads = filter_stale_threads(live, self._config.heat)
        for t in threads:
            t.heat_score = compute_heat(t, self._config.heat)
            t.heat_tier = classify_tier(t.heat_score, self._config.heat)
        return sorted(threads, key=lambda t: t.heat_score, reverse=True)

    def dismiss_thread(self, channel_id: str, thread_ts: str) -> None:
        logger.debug("dismiss_thread: channel=%s thread=%s", channel_id, thread_ts)
        if self._dismiss is not None:
            self._dismiss.dismiss(channel_id, thread_ts)
        # Evict the live entry: it already exists in memory from before the dismiss.
        self._threads.pop((channel_id, thread_ts), None)

    def _evict_threads(self) -> None:
        """Drop entries that should never render again: dismissed keys, and dead
        threads (past max_thread_age_days) that are not currently zombie-eligible.
        Without this, self._threads grows unbounded since nothing else deletes."""
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=self._config.heat.max_thread_age_days)
        now_ts = now.timestamp()
        to_evict: list[tuple[str, str]] = []
        for key, entry in self._threads.items():
            if self._dismiss is not None and self._dismiss.is_dismissed(*key):
                to_evict.append(key)
                continue
            dead = entry.last_activity <= cutoff
            if dead and not is_zombie(entry, self._config.heat, now_ts):
                to_evict.append(key)
        for key in to_evict:
            del self._threads[key]
        if to_evict:
            logger.debug("_evict_threads: evicted %d entries", len(to_evict))
            # B1: prune the observation store by the SAME last_activity horizon the
            # in-memory map uses (the exact evicted keys), never a static
            # first_observed age. A long-lived active thread (old first_observed,
            # recent last_activity) is not evicted here, so its observed row is
            # never purged-then-restamped and never falsely flagged New.
            if self._observed is not None:
                self._observed.delete(to_evict)

    async def reconcile(self) -> None:
        """Catch up after a Socket Mode reconnect (Phase 6).

        Socket Mode does not replay events missed while disconnected, so every gap is a
        blind window. On reconnect we re-list each channel's threads and re-fetch only the
        ones whose latest reply moved past our stored per-thread watermark - recovering new
        replies on old/evicted parents that the watermark-based history poll never surfaces.
        """
        logger.info(
            "reconcile: catching up across %d channels after reconnect", len(self._channel_map)
        )
        total = 0
        for channel_name, channel_id in self._channel_map.items():
            total += await self._reconcile_channel(channel_id, channel_name)
        logger.info("reconcile: re-fetched %d changed threads", total)

    async def _reconcile_channel(self, channel_id: str, channel_name: str) -> int:
        logger.debug("_reconcile_channel: channel=%s", channel_name)
        # No oldest: we must see OLD parents whose latest_reply moved, not just new parents.
        min_replies = resolve_min_replies(channel_name, self._config.fetch)
        thread_messages = await self._slack.fetch_threads(
            channel_id, min_replies=min_replies, oldest=None
        )
        changed = 0
        for msg in thread_messages:
            thread_ts = msg.get("thread_ts", msg["ts"])
            key = (channel_id, thread_ts)
            latest_reply = msg.get("latest_reply") or msg.get("ts", "0")
            watermark = self._thread_watermarks.get(key)
            if watermark is None or latest_reply > watermark:
                changed += 1
                await self._fetch_thread(channel_id, channel_name, thread_ts, incremental=False)
        logger.debug(
            "_reconcile_channel: channel=%s changed=%d of %d",
            channel_name,
            changed,
            len(thread_messages),
        )
        return changed

    async def start(self) -> None:
        self._app_start_at = datetime.now(UTC).timestamp()
        self._self_user_id = await self._slack.resolve_self()
        self._channel_map = self._config.channels
        logger.info(
            "Loaded %d channels from config, seeding fetch queue..."
            " app_start_at=%.3f self_user_id=%s",
            len(self._channel_map),
            self._app_start_at,
            self._self_user_id,
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
                self._evict_threads()
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
        # Short-circuit dismissed threads BEFORE the REST call: checking only at
        # insert still burns a rate-limited fetch for a thread we'd discard.
        if self._dismiss is not None and self._dismiss.is_dismissed(channel_id, thread_ts):
            logger.debug("Skipping dismissed thread %s/%s", channel_name, thread_ts)
            return
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
                # Key participants by stable Slack user_id, matching the socket listener
                # (listener.py): the resolved display name is non-unique/mutable and keying
                # on it double-counts a user active via both paths. See design 2026-06-27.
                user = r.get("user")
                if user:
                    existing.participants[user] = existing.participants.get(user, 0) + 1
            existing.message_count += len(new_replies)
            # Capture the resurrection gap BEFORE last_activity is overwritten (see
            # State merge contract): once we bump last_activity the prior value is gone.
            last_activity = datetime.fromtimestamp(float(latest_ts), tz=UTC)
            prior_ts = existing.last_activity.timestamp()
            event_ts = last_activity.timestamp()
            if detect_resurrection(prior_ts, event_ts, self._config.heat):
                existing.resurrection_event_ts = event_ts
            # Build ReplyRecord objects for the new replies and merge into existing.
            incoming_records = [
                ReplyRecord(
                    ts=float(r["ts"]),
                    author_id=r.get("user", ""),
                    text=(r.get("text", "") or "")[:REPLY_TEXT_MAX],
                    is_root=False,
                )
                for r in new_replies
                if r.get("ts")
            ]
            existing.replies = merge_replies(existing.replies, incoming_records)
            if existing.first_seen_ts <= 0:
                existing.first_seen_ts = float(thread_ts)
            if last_activity > existing.last_activity:
                existing.last_activity = last_activity
            self._update_heat(existing)
            reply_texts = [r.get("text", "") for r in new_replies if r.get("text")]
            if reply_texts:
                self._maybe_trigger_llm(existing, reply_texts)
            return

        participants: dict[str, int] = {}
        for r in replies:
            # Key by stable Slack user_id (see incremental path above and listener.py);
            # started_by below keeps the resolved display name for attribution.
            user = r.get("user")
            if user:
                participants[user] = participants.get(user, 0) + 1
        last_activity = datetime.fromtimestamp(float(latest_ts), tz=UTC)
        first_message = replies[0].get("text", "") if replies else ""
        started_by_id = replies[0].get("user", "") if replies else ""
        started_by_name = (
            existing.started_by
            if existing and existing.started_by
            else await self._resolve_user(started_by_id)
        )
        # Build ReplyRecord objects for the full reply set fetched from Slack.
        # replies[0] is the root message; mark it with is_root=True.
        fetched_records: list[ReplyRecord] = []
        for idx, r in enumerate(replies):
            ts_val = r.get("ts")
            if not ts_val:
                continue
            fetched_records.append(
                ReplyRecord(
                    ts=float(ts_val),
                    author_id=r.get("user", ""),
                    text=(r.get("text", "") or "")[:REPLY_TEXT_MAX],
                    is_root=(idx == 0),
                )
            )
        # State merge contract: the full-fetch rebuild must not silently wipe velocity or
        # resurrection state. first_seen_ts is deterministic from thread_ts.
        # replies merges carried-forward + fetched (deduped by merge_replies).
        carried_records = list(existing.replies) if existing else []
        merged_replies = merge_replies(carried_records, fetched_records)
        # Resurrection is reconstructed state-independently from the full reply timeline
        # (Phase 6): the prior in-memory state is gone after eviction/restart, so carrying
        # forward would miss exactly the long-dead-thread case resurrection exists for.
        # We carry forward an existing event only as a fallback when the reply window is
        # too short to contain the gap (incremental-built entries with sparse timestamps).
        all_reply_ts = sorted(float(r["ts"]) for r in replies if r.get("ts"))
        resurrection_event_ts = reconstruct_resurrection(all_reply_ts, self._config.heat)
        if resurrection_event_ts == 0.0 and existing:
            resurrection_event_ts = existing.resurrection_event_ts
        # The single thread-creation chokepoint: stamp first-observed once (write-once
        # via the store). Degraded/absent store falls back to thread_ts (creation time),
        # which is the cheapest "New" proxy when no observation timestamp is available.
        first_observed_at = (
            self._observed.stamp(channel_id, thread_ts, datetime.now(UTC).timestamp())
            if self._observed
            else float(thread_ts)
        )
        entry = ThreadEntry(
            channel_id=channel_id,
            channel_name=channel_name,
            thread_ts=thread_ts,
            first_message=first_message,
            started_by=started_by_name,
            message_count=len(replies),
            participants=participants,
            last_activity=last_activity,
            title=existing.title if existing else None,
            title_watermark=existing.title_watermark if existing else 0,
            summary=existing.summary if existing else None,
            summary_watermark=existing.summary_watermark if existing else 0,
            replies=merged_replies,
            resurrection_event_ts=resurrection_event_ts,
            first_seen_ts=float(thread_ts),
            first_observed_at=first_observed_at,
            heated_tone=existing.heated_tone if existing else 0,
        )
        self._update_heat(entry)
        self._threads[key] = entry
        logger.debug(
            "Thread %s/%s: %d messages, %d reply records, %d participants, heat=%.1f, last=%s",
            channel_name,
            thread_ts,
            entry.message_count,
            len(entry.replies),
            len(entry.participants),
            entry.heat_score,
            entry.last_activity.isoformat(),
        )
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
        min_replies = resolve_min_replies(channel_name, self._config.fetch)
        thread_messages = await self._slack.fetch_threads(
            channel_id, min_replies=min_replies, oldest=oldest
        )

        if thread_messages:
            latest_ts = max(msg.get("ts", "0") for msg in thread_messages)
            existing_wm = self._channel_watermarks.get(channel_id, "0")
            if latest_ts > existing_wm:
                self._channel_watermarks[channel_id] = latest_ts

        now = datetime.now(UTC)
        age_cutoff = (now - timedelta(days=self._config.heat.max_thread_age_days)).timestamp()
        active = [
            m
            for m in thread_messages
            if float(m.get("latest_reply", m.get("ts", "0"))) >= age_cutoff
        ]
        active.sort(key=lambda m: m.get("reply_count", 0), reverse=True)
        logger.info(
            "Channel %s: %d threads total, %d active in last %dd",
            channel_name,
            len(thread_messages),
            len(active),
            self._config.heat.max_thread_age_days,
        )
        for msg in active:
            thread_ts = msg.get("thread_ts", msg["ts"])
            await self._fetch_thread(channel_id, channel_name, thread_ts, incremental=incremental)

    async def _resolve_user(self, user_id: str) -> str:
        if not user_id:
            return ""
        return await self._slack.resolve_user(user_id)

    def _update_heat(self, entry: ThreadEntry) -> None:
        entry.heat_score = compute_heat(entry, self._config.heat)
        entry.heat_tier = classify_tier(entry.heat_score, self._config.heat)

    def _maybe_trigger_llm(self, entry: ThreadEntry, reply_texts: list[str]) -> None:
        if self._on_title_needed and entry.needs_retitle(
            self._config.heat.retitle_reply_growth,
            self._config.heat.retitle_reply_percent,
        ):
            asyncio.create_task(self._on_title_needed(entry, reply_texts))
        needs_summary = entry.summary is None or entry.message_count > entry.summary_watermark
        if self._on_summary_needed and needs_summary:
            asyncio.create_task(self._on_summary_needed(entry, reply_texts))
