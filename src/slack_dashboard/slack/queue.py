import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PRIORITY_SOCKET_EVENT = 0
PRIORITY_BACKFILL = 10
PRIORITY_REFRESH = 20


@dataclass(order=True)
class FetchItem:
    priority: int
    channel_id: str = field(compare=False)
    channel_name: str = field(compare=False)
    thread_ts: str | None = field(default=None, compare=False)


class FetchQueue:
    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue[FetchItem] = asyncio.PriorityQueue()
        self._pending: set[tuple[str, str | None]] = set()

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    def enqueue(self, item: FetchItem) -> bool:
        key = (item.channel_id, item.thread_ts)
        if key in self._pending:
            return False
        self._pending.add(key)
        self._queue.put_nowait(item)
        return True

    async def dequeue(self) -> FetchItem:
        item = await self._queue.get()
        key = (item.channel_id, item.thread_ts)
        self._pending.discard(key)
        return item

    def seed_channels(self, channels: dict[str, str], priority: int = PRIORITY_BACKFILL) -> int:
        count = 0
        for name, channel_id in channels.items():
            item = FetchItem(
                priority=priority,
                channel_id=channel_id,
                channel_name=name,
            )
            if self.enqueue(item):
                count += 1
        return count
