import json
from pathlib import Path

from slack_dashboard.dismiss import DismissStore


def test_load_empty_when_no_file(tmp_path: Path) -> None:
    store = DismissStore(tmp_path / "dismissed.jsonl")
    store.load()
    assert store.dismissed == set()


def test_dismiss_persists_and_reloads(tmp_path: Path) -> None:
    path = tmp_path / "dismissed.jsonl"
    store = DismissStore(path)
    store.dismiss("C123", "1718900000.000100")
    assert store.is_dismissed("C123", "1718900000.000100")

    # A fresh store loaded from the same file sees the dismissal (survives restart)
    reloaded = DismissStore(path)
    reloaded.load()
    assert reloaded.is_dismissed("C123", "1718900000.000100")


def test_is_dismissed_false_for_unknown(tmp_path: Path) -> None:
    store = DismissStore(tmp_path / "dismissed.jsonl")
    assert not store.is_dismissed("C999", "9999.9999")


def test_dismiss_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "dismissed.jsonl"
    store = DismissStore(path)
    store.dismiss("C1", "1.1")
    store.dismiss("C1", "1.1")
    # Only one record written
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    assert len(lines) == 1


def test_record_has_status_discriminator(tmp_path: Path) -> None:
    path = tmp_path / "dismissed.jsonl"
    store = DismissStore(path)
    store.dismiss("C1", "1.1")
    record = json.loads(path.read_text().splitlines()[0])
    assert record["status"] == "dismissed"
    assert record["channel_id"] == "C1"
    assert record["thread_ts"] == "1.1"
    assert "dismissed_at" in record


def test_load_defaults_missing_status_to_dismissed(tmp_path: Path) -> None:
    path = tmp_path / "dismissed.jsonl"
    path.write_text(json.dumps({"channel_id": "C1", "thread_ts": "1.1"}) + "\n")
    store = DismissStore(path)
    store.load()
    assert store.is_dismissed("C1", "1.1")


def test_load_skips_non_dismissed_status(tmp_path: Path) -> None:
    # Forward-compat: a future "snoozed"/"acknowledged" record is not a dismissal
    path = tmp_path / "dismissed.jsonl"
    path.write_text(
        json.dumps({"channel_id": "C1", "thread_ts": "1.1", "status": "snoozed"}) + "\n"
    )
    store = DismissStore(path)
    store.load()
    assert not store.is_dismissed("C1", "1.1")


def test_load_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "dismissed.jsonl"
    path.write_text("not json\n" + json.dumps({"channel_id": "C1", "thread_ts": "1.1"}) + "\n")
    store = DismissStore(path)
    store.load()
    assert store.is_dismissed("C1", "1.1")
