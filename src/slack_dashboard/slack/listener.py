import logging
from datetime import UTC, datetime
from typing import Any

from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from slack_dashboard.config import HeatConfig
from slack_dashboard.heat import detect_resurrection, prune_timestamps
from slack_dashboard.slack.queue import PRIORITY_SOCKET_EVENT, FetchItem, FetchQueue
from slack_dashboard.thread import ThreadEntry

logger = logging.getLogger(__name__)


class SocketListener:
    def __init__(
        self,
        queue: FetchQueue,
        threads: dict[tuple[str, str], ThreadEntry],
        channel_ids: set[str],
        channel_names: dict[str, str],
        heat_config: HeatConfig,
    ) -> None:
        self._queue = queue
        self._threads = threads
        self._channel_ids = channel_ids
        self._channel_names = channel_names
        self._heat_config = heat_config

    async def handle_event(self, client: AsyncBaseSocketModeClient, req: SocketModeRequest) -> None:
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

        if req.type != "events_api":
            return

        event = req.payload.get("event", {})
        event_type = event.get("type", "")
        if event_type != "message":
            return

        channel_id = event.get("channel", "")
        if channel_id not in self._channel_ids:
            return

        thread_ts = event.get("thread_ts")
        if thread_ts is None:
            return

        channel_name = self._channel_names.get(channel_id, channel_id)
        self._apply_event(channel_id, channel_name, thread_ts, event)

        self._queue.enqueue(
            FetchItem(
                priority=PRIORITY_SOCKET_EVENT,
                channel_id=channel_id,
                channel_name=channel_name,
                thread_ts=thread_ts,
            )
        )

    def _apply_event(
        self,
        channel_id: str,
        channel_name: str,
        thread_ts: str,
        event: dict[str, Any],
    ) -> None:
        key = (channel_id, thread_ts)
        existing = self._threads.get(key)
        if existing is None:
            return

        user = event.get("user")
        if user:
            existing.participants[user] = existing.participants.get(user, 0) + 1

        existing.reply_count += 1

        ts = event.get("ts", "")
        if ts:
            event_time = datetime.fromtimestamp(float(ts), tz=UTC)
            event_ts = event_time.timestamp()
            # Capture the resurrection gap HERE, before last_activity is overwritten.
            # The listener fires the instant an event arrives, ahead of the enqueued
            # fetch; if the poller read last_activity afterward the prior value would
            # already be gone and resurrection could never trip on live events.
            prior_ts = existing.last_activity.timestamp()
            if detect_resurrection(prior_ts, event_ts, self._heat_config):
                existing.resurrection_event_ts = event_ts
            existing.reply_timestamps = prune_timestamps(
                existing.reply_timestamps + [event_ts], self._heat_config
            )
            if existing.first_seen_ts <= 0:
                existing.first_seen_ts = float(thread_ts)
            if event_time > existing.last_activity:
                existing.last_activity = event_time
