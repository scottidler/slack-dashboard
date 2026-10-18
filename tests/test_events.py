import json
from pathlib import Path

from slack_dashboard.events import EventStore


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_record_appends_one_json_line_per_event(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.jsonl")
    store.record("open", "C1", "1.1", rank=3, score=12.5, on_board=True)
    store.record("dismiss", "C1", "1.1", on_board=False)
    rows = _lines(store.path)
    assert [r["kind"] for r in rows] == ["open", "dismiss"]
    assert rows[0]["channel_id"] == "C1"
    assert rows[0]["thread_ts"] == "1.1"
    assert rows[0]["rank"] == 3
    assert rows[0]["score"] == 12.5
    assert isinstance(rows[0]["ts"], float)
    assert isinstance(rows[0]["at"], str)
    assert rows[1]["on_board"] is False


def test_record_creates_the_parent_directory(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "nested" / "dir" / "events.jsonl")
    store.record("open", "C1", "1.1")
    assert len(_lines(store.path)) == 1


def test_record_never_raises_on_a_write_failure(tmp_path: Path) -> None:
    # The path is a directory, so the append fails with an OSError; a request handler
    # must never see it.
    blocked = tmp_path / "events.jsonl"
    blocked.mkdir()
    EventStore(blocked).record("open", "C1", "1.1")
