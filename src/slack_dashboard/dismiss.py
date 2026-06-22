import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class DismissStore:
    """Append-only JSONL store of permanently dismissed threads.

    This is the only state that must survive a restart; everything else is
    rebuilt from Slack on backfill. Each record carries a ``status``
    discriminator (a forward-compat hook for future ack/snooze states); v1 only
    ever writes ``"dismissed"`` and the loader defaults a missing status to it.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._dismissed: set[tuple[str, str]] = set()

    @property
    def dismissed(self) -> set[tuple[str, str]]:
        return self._dismissed

    def load(self) -> None:
        logger.debug("DismissStore.load: path=%s", self._path)
        if not self._path.exists():
            logger.debug("DismissStore.load: no file yet, starting empty")
            return
        count = 0
        for line in self._path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("DismissStore.load: skipping malformed line: %s", line)
                continue
            status = record.get("status", "dismissed")
            if status != "dismissed":
                continue
            channel_id = record.get("channel_id")
            thread_ts = record.get("thread_ts")
            if channel_id and thread_ts:
                self._dismissed.add((channel_id, thread_ts))
                count += 1
        logger.debug("DismissStore.load: loaded %d dismissed keys", count)

    def is_dismissed(self, channel_id: str, thread_ts: str) -> bool:
        return (channel_id, thread_ts) in self._dismissed

    def dismiss(self, channel_id: str, thread_ts: str) -> None:
        logger.debug("DismissStore.dismiss: channel=%s thread=%s", channel_id, thread_ts)
        key = (channel_id, thread_ts)
        if key in self._dismissed:
            logger.debug("DismissStore.dismiss: already dismissed, no-op")
            return
        record = {
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "status": "dismissed",
            "dismissed_at": datetime.now(UTC).isoformat(),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Append atomically: flush + fsync so a crash mid-write can't corrupt the
        # permanent record.
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._dismissed.add(key)
        logger.debug("DismissStore.dismiss: persisted, total=%d", len(self._dismissed))
