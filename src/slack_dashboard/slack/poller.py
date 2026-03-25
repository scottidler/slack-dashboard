import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any

from slack_dashboard.config import AppConfig
from slack_dashboard.heat import classify_tier, compute_heat, prune_threads
from slack_dashboard.slack.client import SlackClient
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
        self._task: asyncio.Task[None] | None = None

    @property
    def threads(self) -> dict[tuple[str, str], ThreadEntry]:
        return self._threads

    def ranked_threads(self) -> list[ThreadEntry]:
        threads = list(self._threads.values())
        return sorted(threads, key=lambda t: t.heat_score, reverse=True)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        try:
            self._channel_map = self._config.channels
            logger.info(
                "Loaded %d channels from config, starting initial fetch...", len(self._channel_map)
            )
            await self._initial_fetch()
            logger.info("Initial fetch complete, entering poll loop")
            await self._poll_loop()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Fatal error in poller")

    async def _initial_fetch(self) -> None:
        for name, channel_id in self._channel_map.items():
            await self._fetch_channel(channel_id, name)
            await asyncio.sleep(1.0)

    async def _poll_loop(self) -> None:
        while True:
            try:
                self._threads = {
                    k: v
                    for k, v in self._threads.items()
                    if v in prune_threads(list(self._threads.values()), self._config.pruning)
                }
                for name, channel_id in self._channel_map.items():
                    await self._fetch_channel(channel_id, name)
                min_interval = self._config.polling.cold_interval_seconds
                for thread in self._threads.values():
                    if thread.heat_tier == "hot":
                        min_interval = min(min_interval, self._config.polling.hot_interval_seconds)
                    elif thread.heat_tier == "warm":
                        min_interval = min(min_interval, self._config.polling.warm_interval_seconds)
                await asyncio.sleep(min_interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in poll loop")
                await asyncio.sleep(30)

    async def _fetch_channel(self, channel_id: str, channel_name: str) -> None:
        try:
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
                entry.heat_score = compute_heat(entry, self._config.heat)
                now = datetime.now(UTC)
                minutes_inactive = (now - entry.last_activity).total_seconds() / 60
                entry.heat_tier = classify_tier(
                    entry.heat_score,
                    self._config.heat,
                    minutes_inactive=minutes_inactive,
                    cold_threshold_minutes=self._config.polling.cold_threshold_minutes,
                )
                self._threads[key] = entry
                reply_texts = [r.get("text", "") for r in replies if r.get("text")]
                if self._on_title_needed and entry.needs_retitle(
                    self._config.heat.retitle_reply_growth,
                    self._config.heat.retitle_reply_percent,
                ):
                    asyncio.create_task(self._on_title_needed(entry, reply_texts))
                needs_summary = entry.summary is None or entry.reply_count > entry.summary_watermark
                if self._on_summary_needed and needs_summary:
                    asyncio.create_task(self._on_summary_needed(entry, reply_texts))
        except Exception:
            logger.exception("Error fetching channel %s", channel_name)
