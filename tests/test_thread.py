from datetime import UTC, datetime

from slack_dashboard.thread import ReplyRecord, ThreadEntry, merge_replies


def test_create_thread_entry() -> None:
    entry = ThreadEntry(
        channel_id="C123",
        channel_name="sre-internal",
        thread_ts="1234567890.123456",
        first_message="Something broke in prod",
        started_by="U1",
        message_count=10,
        participants={"U1": 3, "U2": 2, "U3": 1},
        last_activity=datetime(2026, 3, 24, 12, 0, 0, tzinfo=UTC),
    )
    assert entry.channel_id == "C123"
    assert entry.channel_name == "sre-internal"
    assert entry.thread_ts == "1234567890.123456"
    assert entry.message_count == 10
    assert len(entry.participants) == 3
    assert entry.title is None
    assert entry.summary is None
    assert entry.title_watermark == 0
    assert entry.summary_watermark == 0


def test_thread_entry_defaults() -> None:
    entry = ThreadEntry(
        channel_id="C123",
        channel_name="general",
        thread_ts="1111111111.111111",
        first_message="Hello",
        started_by="U1",
        message_count=0,
        participants={},
        last_activity=datetime(2026, 3, 24, 12, 0, 0, tzinfo=UTC),
    )
    assert entry.heat_score == 0.0
    assert entry.heat_tier == "cold"
    assert entry.title is None
    assert entry.summary is None
    assert entry.heated_tone == 0


def test_display_title_with_llm_title() -> None:
    entry = ThreadEntry(
        channel_id="C123",
        channel_name="general",
        thread_ts="1111111111.111111",
        first_message="This is a very long message that should be truncated",
        started_by="U1",
        message_count=5,
        participants={"U1": 1},
        last_activity=datetime(2026, 3, 24, 12, 0, 0, tzinfo=UTC),
        title="Prod Outage Discussion",
    )
    assert entry.display_title == "Prod Outage Discussion"


def test_display_title_fallback() -> None:
    entry = ThreadEntry(
        channel_id="C123",
        channel_name="general",
        thread_ts="1111111111.111111",
        first_message="This is a very long message that should be truncated to a reasonable length",
        started_by="U1",
        message_count=5,
        participants={"U1": 1},
        last_activity=datetime(2026, 3, 24, 12, 0, 0, tzinfo=UTC),
    )
    assert entry.display_title == entry.first_message[:80]


def test_needs_retitle_no_existing_title() -> None:
    entry = ThreadEntry(
        channel_id="C123",
        channel_name="general",
        thread_ts="1111111111.111111",
        first_message="Hello",
        started_by="U1",
        message_count=5,
        participants={"U1": 1},
        last_activity=datetime(2026, 3, 24, 12, 0, 0, tzinfo=UTC),
    )
    assert entry.needs_retitle(retitle_reply_growth=5, retitle_reply_percent=25)


def test_needs_retitle_sufficient_growth() -> None:
    entry = ThreadEntry(
        channel_id="C123",
        channel_name="general",
        thread_ts="1111111111.111111",
        first_message="Hello",
        started_by="U1",
        message_count=30,
        participants={"U1": 1},
        last_activity=datetime(2026, 3, 24, 12, 0, 0, tzinfo=UTC),
        title="Existing Title",
        title_watermark=20,
    )
    # new_replies = 30 - 20 = 10, threshold = max(5, 20 * 25 / 100) = max(5, 5) = 5
    assert entry.needs_retitle(retitle_reply_growth=5, retitle_reply_percent=25)


def test_needs_retitle_insufficient_growth() -> None:
    entry = ThreadEntry(
        channel_id="C123",
        channel_name="general",
        thread_ts="1111111111.111111",
        first_message="Hello",
        started_by="U1",
        message_count=22,
        participants={"U1": 1},
        last_activity=datetime(2026, 3, 24, 12, 0, 0, tzinfo=UTC),
        title="Existing Title",
        title_watermark=20,
    )
    # new_replies = 22 - 20 = 2, threshold = max(5, 20 * 25 / 100) = max(5, 5) = 5
    assert not entry.needs_retitle(retitle_reply_growth=5, retitle_reply_percent=25)


# ---------------------------------------------------------------------------
# ReplyRecord + merge_replies tests
# ---------------------------------------------------------------------------


def test_reply_timestamps_is_derived_projection() -> None:
    """reply_timestamps is a property that projects ts from replies."""
    entry = ThreadEntry(
        channel_id="C1",
        channel_name="test",
        thread_ts="100.000000",
        first_message="root",
        started_by="U1",
        message_count=3,
        participants={"U1": 2, "U2": 1},
        last_activity=datetime(2026, 1, 1, tzinfo=UTC),
    )
    entry.replies = [
        ReplyRecord(ts=100.0, author_id="U1", text="root", is_root=True),
        ReplyRecord(ts=200.0, author_id="U2", text="reply 1", is_root=False),
        ReplyRecord(ts=300.0, author_id="U1", text="reply 2", is_root=False),
    ]
    assert entry.reply_timestamps == [100.0, 200.0, 300.0]


def test_reply_timestamps_empty_when_no_replies() -> None:
    entry = ThreadEntry(
        channel_id="C1",
        channel_name="test",
        thread_ts="100.000000",
        first_message="root",
        started_by="U1",
        message_count=1,
        participants={"U1": 1},
        last_activity=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert entry.reply_timestamps == []


def test_merge_replies_dedupes_by_normalized_ts() -> None:
    """Same ts with sub-ulp float difference should collapse to one record."""
    r1 = ReplyRecord(ts=1000.123456, author_id="U1", text="hi", is_root=False)
    # Slightly different float that rounds to same 6dp key
    r2 = ReplyRecord(ts=1000.1234561, author_id="U1", text="hi updated", is_root=False)
    result = merge_replies([r1], [r2])
    assert len(result) == 1
    # latest wins: r2 is in incoming, so it wins
    assert result[0].text == "hi updated"


def test_merge_replies_ordered_by_ts() -> None:
    """Result must be sorted ascending by ts regardless of insertion order."""
    records = [
        ReplyRecord(ts=300.0, author_id="U1", text="c", is_root=False),
        ReplyRecord(ts=100.0, author_id="U2", text="a", is_root=True),
        ReplyRecord(ts=200.0, author_id="U1", text="b", is_root=False),
    ]
    result = merge_replies([], records)
    assert [r.ts for r in result] == [100.0, 200.0, 300.0]


def test_merge_replies_caps_at_max() -> None:
    """Results are capped at MAX_REPLY_RECORDS; oldest are dropped."""
    from slack_dashboard.thread import MAX_REPLY_RECORDS

    # Build more records than the cap
    many = [
        ReplyRecord(ts=float(i), author_id="U1", text=f"msg {i}", is_root=False)
        for i in range(MAX_REPLY_RECORDS + 10)
    ]
    result = merge_replies([], many)
    assert len(result) == MAX_REPLY_RECORDS
    # Oldest should be dropped; youngest retained
    assert result[0].ts == float(10)  # first 10 (oldest) dropped
    assert result[-1].ts == float(MAX_REPLY_RECORDS + 9)


def test_merge_replies_new_wins_for_same_ts() -> None:
    """When existing and incoming share a ts key, incoming (latest) wins."""
    existing = [ReplyRecord(ts=100.0, author_id="U1", text="old", is_root=False)]
    incoming = [ReplyRecord(ts=100.0, author_id="U1", text="new", is_root=False)]
    result = merge_replies(existing, incoming)
    assert len(result) == 1
    assert result[0].text == "new"


def test_merge_replies_combines_disjoint_records() -> None:
    """Disjoint ts sets should all be present in merged output."""
    existing = [
        ReplyRecord(ts=100.0, author_id="U1", text="a", is_root=True),
        ReplyRecord(ts=200.0, author_id="U2", text="b", is_root=False),
    ]
    incoming = [
        ReplyRecord(ts=300.0, author_id="U1", text="c", is_root=False),
        ReplyRecord(ts=400.0, author_id="U2", text="d", is_root=False),
    ]
    result = merge_replies(existing, incoming)
    assert len(result) == 4
    assert [r.ts for r in result] == [100.0, 200.0, 300.0, 400.0]


def test_merge_replies_empty_incoming() -> None:
    existing = [ReplyRecord(ts=100.0, author_id="U1", text="a", is_root=True)]
    result = merge_replies(existing, [])
    assert len(result) == 1
    assert result[0].ts == 100.0


def test_merge_replies_empty_existing() -> None:
    incoming = [ReplyRecord(ts=100.0, author_id="U1", text="a", is_root=True)]
    result = merge_replies([], incoming)
    assert len(result) == 1
    assert result[0].ts == 100.0
