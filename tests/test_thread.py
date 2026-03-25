from datetime import UTC, datetime

from slack_dashboard.thread import ThreadEntry


def test_create_thread_entry() -> None:
    entry = ThreadEntry(
        channel_id="C123",
        channel_name="sre-internal",
        thread_ts="1234567890.123456",
        first_message="Something broke in prod",
        reply_count=10,
        participants={"U1", "U2", "U3"},
        last_activity=datetime(2026, 3, 24, 12, 0, 0, tzinfo=UTC),
    )
    assert entry.channel_id == "C123"
    assert entry.channel_name == "sre-internal"
    assert entry.thread_ts == "1234567890.123456"
    assert entry.reply_count == 10
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
        reply_count=0,
        participants=set(),
        last_activity=datetime(2026, 3, 24, 12, 0, 0, tzinfo=UTC),
    )
    assert entry.heat_score == 0.0
    assert entry.heat_tier == "cold"
    assert entry.title is None
    assert entry.summary is None


def test_display_title_with_llm_title() -> None:
    entry = ThreadEntry(
        channel_id="C123",
        channel_name="general",
        thread_ts="1111111111.111111",
        first_message="This is a very long message that should be truncated",
        reply_count=5,
        participants={"U1"},
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
        reply_count=5,
        participants={"U1"},
        last_activity=datetime(2026, 3, 24, 12, 0, 0, tzinfo=UTC),
    )
    assert entry.display_title == entry.first_message[:80]


def test_needs_retitle_no_existing_title() -> None:
    entry = ThreadEntry(
        channel_id="C123",
        channel_name="general",
        thread_ts="1111111111.111111",
        first_message="Hello",
        reply_count=5,
        participants={"U1"},
        last_activity=datetime(2026, 3, 24, 12, 0, 0, tzinfo=UTC),
    )
    assert entry.needs_retitle(retitle_reply_growth=5, retitle_reply_percent=25)


def test_needs_retitle_sufficient_growth() -> None:
    entry = ThreadEntry(
        channel_id="C123",
        channel_name="general",
        thread_ts="1111111111.111111",
        first_message="Hello",
        reply_count=30,
        participants={"U1"},
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
        reply_count=22,
        participants={"U1"},
        last_activity=datetime(2026, 3, 24, 12, 0, 0, tzinfo=UTC),
        title="Existing Title",
        title_watermark=20,
    )
    # new_replies = 22 - 20 = 2, threshold = max(5, 20 * 25 / 100) = max(5, 5) = 5
    assert not entry.needs_retitle(retitle_reply_growth=5, retitle_reply_percent=25)
