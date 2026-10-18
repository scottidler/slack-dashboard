import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from slack_dashboard.config import AppConfig, HeatConfig, resolve_min_replies
from slack_dashboard.dismiss import DismissStore
from slack_dashboard.events import EventStore
from slack_dashboard.heat import (
    classify_tier,
    compute_heat,
    detect_resurrection,
    filter_stale_threads,
    is_zombie,
    reconstruct_resurrection,
    score_threads,
    write_back,
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
        on_topics_needed: Any | None = None,
        dismiss: DismissStore | None = None,
        observed: ObservedStore | None = None,
        events: EventStore | None = None,
    ) -> None:
        self._slack = slack_client
        self._config = config
        self._on_title_needed = on_title_needed
        self._on_summary_needed = on_summary_needed
        self._on_topics_needed = on_topics_needed
        self._dismiss = dismiss
        self._observed = observed
        self._events = events
        self._threads: dict[tuple[str, str], ThreadEntry] = {}
        self._channel_map: dict[str, str] = {}
        self._queue = FetchQueue()
        self._consumer_task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._names_task: asyncio.Task[None] | None = None
        self._worker_semaphore = asyncio.Semaphore(10)
        self._active_workers: set[asyncio.Task[None]] = set()
        self._channel_watermarks: dict[str, str] = {}
        self._thread_watermarks: dict[tuple[str, str], str] = {}
        # Captured once in start() so the render path can suppress the new glyph
        # for the first new_window after poller start (app-start storm suppressor, M2).
        self._app_start_at: float = 0.0
        # The authenticated user's own Slack user_id, resolved once in start() via
        # auth.test. The listener reads it to mark a thread followed the moment I post.
        # None until resolved (or if auth.test fails), in which case no own post is seen.
        self._self_user_id: str | None = None

    @property
    def self_user_id(self) -> str | None:
        """The authenticated user's Slack user_id, or None if unresolved.

        Resolved once in ``start()``; the socket listener reads it (through a callable,
        since the listener is built before ``start()``) to set the follow state on my own
        post. None (auth.test failed/not yet run) means no event is recognized as mine.
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

    def eligible(self, config: HeatConfig, now: float) -> list[ThreadEntry]:
        """The threads a board may show: not dismissed, and active within ``config``'s age.

        Pure (reads the map, writes nothing), so a request scoring under its own config gets
        the same membership as ``ranked_threads`` without calling the mutating wrapper.
        """
        live = [
            t
            for t in self._threads.values()
            if self._dismiss is None or not self._dismiss.is_dismissed(t.channel_id, t.thread_ts)
        ]
        eligible = filter_stale_threads(live, config, now)
        logger.debug(
            "eligible: threads=%d live=%d eligible=%d max_thread_age_days=%d",
            len(self._threads),
            len(live),
            len(eligible),
            config.max_thread_age_days,
        )
        return eligible

    def ranked_threads(self, now: float | None = None) -> list[ThreadEntry]:
        """The yml ranking, written back into the entries (events and the poller's own uses)."""
        if now is None:
            now = datetime.now(UTC).timestamp()
        heat = self._config.heat
        return write_back(score_threads(self.eligible(heat, now), heat, now))

    def dismiss_thread(
        self, channel_id: str, thread_ts: str, overrides: dict[str, str] | None = None
    ) -> None:
        logger.debug(
            "dismiss_thread: channel=%s thread=%s overrides=%s", channel_id, thread_ts, overrides
        )
        self._record_event("dismiss", channel_id, thread_ts, overrides)
        if self._dismiss is not None:
            self._dismiss.dismiss(channel_id, thread_ts)
        # Evict the live entry: it already exists in memory from before the dismiss.
        self._threads.pop((channel_id, thread_ts), None)

    def open_thread(
        self, channel_id: str, thread_ts: str, overrides: dict[str, str] | None = None
    ) -> None:
        """Record that the user clicked through to this thread (the board's "open" event).

        ``overrides`` is the knob map the clicked surface was rendered with; it rides the
        event so the next tuning label can exclude opens made from a board the yml never drew.
        """
        logger.debug(
            "open_thread: channel=%s thread=%s overrides=%s", channel_id, thread_ts, overrides
        )
        self._record_event("open", channel_id, thread_ts, overrides)

    def _record_event(
        self,
        kind: str,
        channel_id: str,
        thread_ts: str,
        overrides: dict[str, str] | None = None,
    ) -> None:
        """Append a board event with where the thread sat on the board at that moment.

        The ranking is recomputed here rather than carried from the last render: the row
        the user saw may be up to one poll old, and the event's job is to say what the
        board held when the user acted, which this is within seconds. The rank is always the
        yml's; a non-empty ``overrides`` is written as ``overrides`` so the label can exclude
        the row, and an empty one writes no such key.
        """
        if self._events is None:
            return
        context: dict[str, Any] = {"on_board": False}
        for rank, entry in enumerate(self.ranked_threads(), start=1):
            if (entry.channel_id, entry.thread_ts) == (channel_id, thread_ts):
                context = {
                    "on_board": True,
                    "channel": entry.channel_name,
                    "rank": rank,
                    "score": round(entry.heat_score, 3),
                    "tier": entry.heat_tier,
                    "message_count": entry.message_count,
                    "topics": entry.topics,
                }
                break
        if overrides:
            context["overrides"] = overrides
        self._events.record(kind, channel_id, thread_ts, **context)

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
        # The knob panel labels people-weights rows by name; the client resolves one user at
        # a time, so warm them here rather than stall the first /knobs after a restart.
        self._names_task = asyncio.create_task(self._warm_people_names())

    async def stop(self) -> None:
        for task in [self._consumer_task, self._refresh_task, self._names_task]:
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
                await self._sweep_metadata()
        except asyncio.CancelledError:
            return

    async def _sweep_metadata(self) -> None:
        """Re-read every channel's parents and refresh the follow state on known threads.

        The incremental fetch never re-reads a quiet thread, so a follow or unfollow made in
        Slack with no new reply would never reach the board. One history call per channel
        (``oldest=None``, paced by the client) returns every parent with ``subscribed`` and
        ``last_read``; this copies them onto the entries without fetching replies. A failed
        channel is logged and skipped so one bad call cannot stop the refresh loop.
        """
        updated = 0
        for channel_name, channel_id in self._channel_map.items():
            try:
                # min_replies=1, not the channel's: a socket-tracked thread below min-replies
                # is on the board too, and its follow state must reach it.
                parents = await self._slack.fetch_threads(channel_id, min_replies=1, oldest=None)
            except Exception:
                logger.exception("_sweep_metadata: history failed for channel=%s", channel_name)
                continue
            for msg in parents:
                # Slack puts the follow state only on parents that have replies.
                if not msg.get("reply_count"):
                    continue
                thread_ts = msg.get("thread_ts", msg.get("ts", ""))
                entry = self._threads.get((channel_id, thread_ts))
                if entry is None:
                    continue
                if _apply_follow_state(entry, msg):
                    updated += 1
        logger.debug(
            "_sweep_metadata: channels=%d entries_updated=%d", len(self._channel_map), updated
        )

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
        # A root-only incremental response would pull the watermark back to thread_ts, and the
        # next fetch would re-add replies already counted.
        watermark = max(latest_ts, oldest, key=float) if oldest else latest_ts
        self._thread_watermarks[key] = watermark

        # The root is the message whose ts == thread_ts, never "the first item": with oldest
        # set the client concatenates pages. It carries Slack's follow state for me.
        root = next((r for r in replies if r.get("ts") == thread_ts), None)
        existing = self._threads.get(key)
        if incremental and existing and oldest:
            if root is None:
                logger.warning(
                    "_fetch_thread: no root in replies for channel=%s thread_ts=%s; "
                    "follow state unchanged",
                    channel_name,
                    thread_ts,
                )
            else:
                _apply_follow_state(existing, root)
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
            topics=existing.topics if existing else None,
            topics_watermark=existing.topics_watermark if existing else 0,
            subscribed=existing.subscribed if existing else False,
            last_read=existing.last_read if existing else None,
        )
        if root is None:
            logger.warning(
                "_fetch_thread: no root in replies for channel=%s thread_ts=%s; "
                "follow state unchanged",
                channel_name,
                thread_ts,
            )
        else:
            _apply_follow_state(entry, root)
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

    async def resolve_user(self, user_id: str) -> str:
        """Display name for a user id (cached by the client; the id itself on failure)."""
        return await self._resolve_user(user_id)

    async def _warm_people_names(self) -> None:
        for user_id in self._config.heat.people_weights:
            await self._resolve_user(user_id)

    def _update_heat(self, entry: ThreadEntry) -> None:
        # Provisional per-entry update on ingest; the authoritative, rank-aware tier is set
        # POST-SORT in ranked_threads at render time. With no board context here we classify
        # the entry as the sole thread (rank 0 of 1), so absolute mode is exact and relative
        # mode gives a best-effort value that ranked_threads later overwrites.
        entry.heat_score = compute_heat(entry, self._config.heat)
        entry.heat_tier = classify_tier(entry.heat_score, 0, 1, self._config.heat)

    def _maybe_trigger_llm(self, entry: ThreadEntry, reply_texts: list[str]) -> None:
        if self._on_title_needed and entry.needs_retitle(
            self._config.heat.retitle_reply_growth,
            self._config.heat.retitle_reply_percent,
        ):
            asyncio.create_task(self._on_title_needed(entry, reply_texts))
        needs_summary = entry.summary is None or entry.message_count > entry.summary_watermark
        if self._on_summary_needed and needs_summary:
            asyncio.create_task(self._on_summary_needed(entry, reply_texts))
        # Topic rating follows the same watermark shape as the summary: once per thread, and
        # again when the thread grows past the last rated size. The callback owns the
        # watermark so a failed call (topics stays None) re-triggers on the next fetch.
        needs_topics = entry.topics is None or entry.message_count > entry.topics_watermark
        if self._on_topics_needed and needs_topics:
            asyncio.create_task(self._on_topics_needed(entry))


def _apply_follow_state(entry: ThreadEntry, root: dict[str, Any]) -> bool:
    """Copy Slack's ``subscribed`` / ``last_read`` from a thread root (or a channel parent).

    Returns True when the fields were updated. A root without the ``subscribed`` key leaves
    both fields as they were and logs a warning: the key was present on every root in the
    Phase 0 probe, so its absence means Slack changed shape, not that I unfollowed.
    """
    if "subscribed" not in root:
        logger.warning(
            "_apply_follow_state: root has no subscribed key channel=%s thread_ts=%s; "
            "follow state unchanged",
            entry.channel_name,
            entry.thread_ts,
        )
        return False
    last_read = root.get("last_read")
    entry.subscribed = bool(root["subscribed"])
    entry.last_read = float(last_read) if last_read else None
    logger.debug(
        "_apply_follow_state: channel=%s thread_ts=%s subscribed=%s last_read=%s",
        entry.channel_name,
        entry.thread_ts,
        entry.subscribed,
        entry.last_read,
    )
    return True
