from pathlib import Path

from slack_dashboard.observed import ObservedStore


def test_restart_survival_sees_original_timestamp(tmp_path: Path) -> None:
    """A second store over the same db sees the FIRST-observed timestamp, not a
    re-stamp on the later open (durable across restart)."""
    db = tmp_path / "observed.db"
    store = ObservedStore(db)
    store.load()
    first = store.stamp("C1", "100.1", now=1000.0)
    assert first == 1000.0

    reloaded = ObservedStore(db)
    reloaded.load()
    # A later open with a different `now` must return the persisted original.
    again = reloaded.stamp("C1", "100.1", now=9999.0)
    assert again == 1000.0


def test_insert_or_ignore_does_not_clobber(tmp_path: Path) -> None:
    """Re-stamping a known key returns the original timestamp; the second `now`
    is ignored (write-once primitive)."""
    store = ObservedStore(tmp_path / "observed.db")
    store.load()
    assert store.stamp("C1", "1.1", now=500.0) == 500.0
    assert store.stamp("C1", "1.1", now=800.0) == 500.0


def test_delete_drops_exactly_evicted_keys(tmp_path: Path) -> None:
    """B1 regression: delete(keys) drops exactly the evicted keys and leaves a
    long-lived active thread (old first_observed, not evicted) intact, so it is
    never re-stamped as falsely New."""
    store = ObservedStore(tmp_path / "observed.db")
    store.load()
    store.stamp("C1", "old-active", now=1.0)  # old first_observed, still active
    store.stamp("C1", "evict-me", now=2.0)
    store.stamp("C2", "evict-me-too", now=3.0)

    deleted = store.delete([("C1", "evict-me"), ("C2", "evict-me-too")])
    assert deleted == 2

    # The long-lived active thread keeps its ORIGINAL stamp (not re-stamped).
    assert store.stamp("C1", "old-active", now=999.0) == 1.0
    # The evicted keys are gone: a fresh stamp takes the new `now`.
    assert store.stamp("C1", "evict-me", now=999.0) == 999.0


def test_delete_empty_is_noop(tmp_path: Path) -> None:
    store = ObservedStore(tmp_path / "observed.db")
    store.load()
    assert store.delete([]) == 0


def test_stamp_degrades_without_raising_when_db_unwritable(tmp_path: Path) -> None:
    """A stamp() against a locked/unwritable db degrades to the per-session mirror
    without raising (M1)."""
    db = tmp_path / "observed.db"
    store = ObservedStore(db)
    store.load()
    # Force the degraded path: close the live connection out from under the store.
    assert store._conn is not None
    store._conn.close()
    # Must not raise; returns the supplied `now` from the per-session mirror.
    value = store.stamp("C1", "1.1", now=42.0)
    assert value == 42.0
    # Mirror still answers a repeat hit without sqlite.
    assert store.stamp("C1", "1.1", now=99.0) == 42.0


def test_delete_degrades_without_raising_when_db_unwritable(tmp_path: Path) -> None:
    """A delete() against a closed db degrades (mirror-only drop) without raising (M1)."""
    db = tmp_path / "observed.db"
    store = ObservedStore(db)
    store.load()
    store.stamp("C1", "1.1", now=10.0)
    assert store._conn is not None
    store._conn.close()
    # Must not raise; the mirror entry is dropped even though sqlite is gone.
    store.delete([("C1", "1.1")])
    # Re-stamp now takes the new value (mirror entry was removed).
    assert store.stamp("C1", "1.1", now=77.0) == 77.0


def test_load_degrades_when_path_unusable(tmp_path: Path) -> None:
    """load() against an unusable path degrades to in-memory-only without raising;
    the store still functions per-session via the mirror."""
    # A directory cannot be opened as a sqlite db file.
    bad = tmp_path / "as-dir"
    bad.mkdir()
    store = ObservedStore(bad)
    store.load()
    assert store._conn is None
    # Degraded mode: stamp uses the per-session mirror, write-once still holds.
    assert store.stamp("C1", "1.1", now=5.0) == 5.0
    assert store.stamp("C1", "1.1", now=6.0) == 5.0
