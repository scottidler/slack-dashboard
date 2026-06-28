import logging
import sqlite3
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

# A deliberately low busy timeout (ms): a locked db fails fast into the
# trap-and-degrade path rather than stalling the poller's event loop.
_BUSY_TIMEOUT_MS = 100

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observed (
    channel_id    TEXT NOT NULL,
    thread_ts     TEXT NOT NULL,
    first_observed REAL NOT NULL,
    PRIMARY KEY (channel_id, thread_ts)
);
CREATE INDEX IF NOT EXISTS idx_observed_first ON observed (first_observed);
"""


class ObservedStore:
    """sqlite3-backed record of when the dashboard FIRST observed each thread.

    Unlike the append-only ``DismissStore``, observed state grows with every
    thread ever seen and must be pruned (bounded by the in-memory eviction
    horizon), so sqlite's atomic write-once (``INSERT OR IGNORE``) and indexed
    ``DELETE`` are used instead of a JSONL rewrite. The connection is opened and
    used on the poller's event-loop thread (no executor offload), so the default
    ``check_same_thread=True`` is safe.

    Every path (load/stamp/delete) traps sqlite errors, logs WARN, and degrades
    to the in-memory mirror; nothing here ever raises into the poller worker. In
    degraded mode (no db) the mirror is per-session only.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None
        # Read mirror: a complete reflection of the db after load(), so a stamp()
        # hit never touches sqlite (the render hot path reads first_observed_at
        # off ThreadEntry, never this store).
        self._mirror: dict[tuple[str, str], float] = {}

    def load(self) -> None:
        """Open/create the db and hydrate the in-memory mirror. On any sqlite
        error OR an OS error creating the directory/file, log WARN and fall back
        to in-memory-only (degraded) mode."""
        logger.debug("ObservedStore.load: path=%s", self._path)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path)
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            conn.executescript(_SCHEMA)
            conn.commit()
            rows = conn.execute("SELECT channel_id, thread_ts, first_observed FROM observed")
            for channel_id, thread_ts, first_observed in rows:
                self._mirror[(channel_id, thread_ts)] = first_observed
            self._conn = conn
            logger.debug("ObservedStore.load: loaded %d observed keys", len(self._mirror))
        except (sqlite3.Error, OSError) as exc:
            # OSError covers an uncreatable/un-permissioned config dir (mkdir above):
            # startup degrades to in-memory-only rather than raising out of load().
            logger.warning("ObservedStore.load: open error, degrading to in-memory only: %s", exc)
            self._conn = None

    def stamp(self, channel_id: str, thread_ts: str, now: float) -> float:
        """Return the thread's first-observed epoch, writing ``now`` once if unseen.

        Reads are mirror-only; a miss does ``INSERT OR IGNORE`` and updates the
        mirror. Any sqlite error is trapped, logged WARN, and the mirror value is
        returned/used. NEVER raises, so a write failure can never crash the poller
        worker (``_process_item``). In degraded mode the mirror is per-session only.
        """
        logger.debug(
            "ObservedStore.stamp: channel=%s thread=%s now=%.3f", channel_id, thread_ts, now
        )
        key = (channel_id, thread_ts)
        existing = self._mirror.get(key)
        if existing is not None:
            return existing
        if self._conn is not None:
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO observed (channel_id, thread_ts, first_observed) "
                    "VALUES (?, ?, ?)",
                    (channel_id, thread_ts, now),
                )
                self._conn.commit()
                row = self._conn.execute(
                    "SELECT first_observed FROM observed WHERE channel_id = ? AND thread_ts = ?",
                    (channel_id, thread_ts),
                ).fetchone()
                if row is not None:
                    now = row[0]
            except sqlite3.Error as exc:
                logger.warning(
                    "ObservedStore.stamp: sqlite error for %s/%s, using mirror value: %s",
                    channel_id,
                    thread_ts,
                    exc,
                )
        self._mirror[key] = now
        return now

    def delete(self, keys: Iterable[tuple[str, str]]) -> int:
        """Drop the given ``(channel_id, thread_ts)`` rows and mirror entries.

        Driven by the EXACT set ``_evict_threads`` removes (a targeted indexed
        DELETE), so the store tracks the in-memory horizon by ``last_activity`` -
        NOT by a static first_observed age, which would purge long-lived active
        threads and re-stamp them as falsely New (Blocker B1). Errors degrade,
        never raise. Returns count.
        """
        key_list = list(keys)
        logger.debug("ObservedStore.delete: keys=%d", len(key_list))
        if not key_list:
            return 0
        deleted = 0
        if self._conn is not None:
            try:
                cursor = self._conn.executemany(
                    "DELETE FROM observed WHERE channel_id = ? AND thread_ts = ?",
                    key_list,
                )
                self._conn.commit()
                deleted = cursor.rowcount if cursor.rowcount >= 0 else 0
            except sqlite3.Error as exc:
                logger.warning(
                    "ObservedStore.delete: sqlite error, dropping from mirror only: %s", exc
                )
        for key in key_list:
            self._mirror.pop(key, None)
        logger.debug("ObservedStore.delete: deleted %d rows", deleted)
        return deleted
