import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class EventStore:
    """Append-only JSONL log of what the user DID on the board: opened a thread, dismissed one.

    The heat model is tuned against a label (which threads needed the user), and the only
    label the Slack history can give is "replied". That label is blind to everything read
    and left alone, which is most of what a director does with a board. These events are the
    missing signal: an ``open`` is the user choosing a row, a ``dismiss`` is the user saying
    "never this one". ``bin/corpus.py`` and ``bin/replay.py`` join them to threads by
    ``(channel_id, thread_ts)``; nothing in the live app reads them back.

    Each record carries the board context at the moment of the action (rank, score, tier)
    so the replay can ask "was it on the board, and where" without reconstructing the look.
    Writes are flush + fsync like ``DismissStore``; a failure is logged and never raises
    into a request handler.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def record(self, kind: str, channel_id: str, thread_ts: str, **context: Any) -> None:
        now = datetime.now(UTC)
        entry = {
            "kind": kind,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "ts": now.timestamp(),
            "at": now.isoformat(),
            **context,
        }
        logger.debug("EventStore.record: %s", entry)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            logger.warning("EventStore.record: write failed for %s: %s", self._path, exc)
