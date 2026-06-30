import time as _time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, PropertyMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from slack_dashboard.config import AppConfig, HeatConfig, SlackConfig
from slack_dashboard.llm.provider import LlmProvider, SummaryResult
from slack_dashboard.slack.poller import SlackPoller
from slack_dashboard.thread import ThreadEntry
from slack_dashboard.web import _emojis, create_routes

# A far-past app_start_at so the storm suppressor window (new_window_minutes * 60)
# is already expired for all Phase 1 and Phase 3 tests that want the storm suppressor
# out of the way. 7200 seconds = 2 hours, well past a 60-min window.
_FAR_PAST_APP_START = _time.time() - 7200
_NOW = _time.time()

_CONFIG = AppConfig(workspace="tatari")


class MockLlm(LlmProvider):
    async def generate_title(self, messages: list[str]) -> str | None:
        return "Mock Title"

    async def generate_summary(self, messages: list[str]) -> SummaryResult:
        return SummaryResult(bullets="Mock summary of the thread.", tone=0)


class FailingLlm(LlmProvider):
    async def generate_title(self, messages: list[str]) -> str | None:
        return None

    async def generate_summary(self, messages: list[str]) -> SummaryResult:
        return SummaryResult(bullets=None, tone=0)


def _make_thread() -> ThreadEntry:
    return ThreadEntry(
        channel_id="C123",
        channel_name="sre-internal",
        thread_ts="1234567890.123456",
        first_message="Something broke in prod",
        started_by="U1",
        message_count=10,
        participants={"U1": 3, "U2": 2, "U3": 1},
        last_activity=datetime(2026, 3, 24, 12, 0, 0, tzinfo=UTC),
        heat_score=80.0,
        heat_tier="hot",
    )


def _make_mock_poller(threads: list[ThreadEntry] | None = None) -> AsyncMock:
    """Create a mock SlackPoller with app_start_at set far in the past (storm suppressor clear)."""
    poller = AsyncMock(spec=SlackPoller)
    # app_start_at is a property; set it as a plain attribute on the mock instance
    # so the storm suppressor (now - app_start_at >= new_window) is always satisfied
    # in route-level tests that are not specifically testing the suppressor.
    type(poller).app_start_at = PropertyMock(return_value=_FAR_PAST_APP_START)
    if threads is not None:
        poller.ranked_threads.return_value = threads
    return poller


@pytest.fixture
def app_with_threads() -> FastAPI:
    app = FastAPI()
    poller = _make_mock_poller()
    thread = _make_thread()
    poller.ranked_threads.return_value = [thread]
    poller.threads = {("C123", "1234567890.123456"): thread}
    create_routes(app, poller, MockLlm(), _CONFIG)
    return app


@pytest.fixture
def client(app_with_threads: FastAPI) -> TestClient:
    return TestClient(app_with_threads)


def test_index_returns_html(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Slack Dashboard" in response.text
    assert "thread-list" in response.text


def test_threads_returns_partial(client: TestClient) -> None:
    response = client.get("/threads")
    assert response.status_code == 200
    assert "sre-internal" in response.text
    assert "Something broke in prod" in response.text
    # Compact counts: "10m" messages, "3p" participants
    assert "10m" in response.text
    assert "3p" in response.text


def test_threads_renders_deep_link(client: TestClient) -> None:
    response = client.get("/threads")
    # Web deep link: thread_ts without the dot, p-prefixed
    assert "https://tatari.slack.com/archives/C123/p1234567890123456" in response.text
    # Web links open in a new tab.
    assert 'target="_blank"' in response.text


def test_threads_app_link_has_no_target_blank() -> None:
    # With a team id the row link is a slack:// app handoff; it opens in place, so no
    # target="_blank" (which would otherwise orphan an empty browser tab on every click).
    app = FastAPI()
    poller = _make_mock_poller([_make_thread()])
    config = AppConfig(slack=SlackConfig(team_id="T999"))
    create_routes(app, poller, MockLlm(), config)
    response = TestClient(app).get("/threads")
    assert "slack://channel?team=T999" in response.text
    assert 'target="_blank"' not in response.text


def test_summarize_quotes_first_message_and_author(client: TestClient) -> None:
    # The detail header quotes the thread's first message and attributes it to the author.
    response = client.get("/summarize/C123/1234567890.123456")
    assert "Something broke in prod" in response.text
    assert "U1" in response.text
    assert "Mock summary" in response.text  # bullets still render below the quote


def test_channel_route_lists_ranked_threads(client: TestClient) -> None:
    response = client.get("/channel/C123")
    assert response.status_code == 200
    assert "#sre-internal" in response.text
    assert "Something broke in prod" in response.text
    # Each listing is a clickable Slack link (web form here: no team id configured).
    assert "https://tatari.slack.com/archives/C123" in response.text


def test_channel_route_app_links_when_team_id_set() -> None:
    app = FastAPI()
    poller = _make_mock_poller([_make_thread()])
    config = AppConfig(slack=SlackConfig(team_id="T999"))
    create_routes(app, poller, MockLlm(), config)
    response = TestClient(app).get("/channel/C123")
    # Both the channel header and each thread listing hand off to the desktop app.
    # (& renders HTML-escaped as &amp; in the href attribute; the browser decodes it.)
    assert "slack://channel?team=T999&amp;id=C123" in response.text


def test_threads_channel_name_is_link(client: TestClient) -> None:
    # The #channel handle in the MAIN view is itself a link to the channel (web form
    # here: workspace set, no team id).
    response = client.get("/threads")
    assert '<a class="channel-badge" href="https://tatari.slack.com/archives/C123"' in response.text


def test_threads_channel_link_uses_app_scheme_when_team_id_set() -> None:
    app = FastAPI()
    poller = _make_mock_poller([_make_thread()])
    config = AppConfig(slack=SlackConfig(team_id="T999"))
    create_routes(app, poller, MockLlm(), config)
    response = TestClient(app).get("/threads")
    # Channel handle opens the channel root in the desktop app (no &message=).
    assert 'href="slack://channel?team=T999&amp;id=C123"' in response.text


def test_threads_renders_fire_emoji_for_hot(client: TestClient) -> None:
    # The fixture thread is heat_tier="hot"
    response = client.get("/threads")
    assert "\N{FIRE}" in response.text


def _client_with_n_threads(n: int) -> TestClient:
    app = FastAPI()
    threads = []
    for i in range(n):
        t = _make_thread()
        t.thread_ts = f"100000000{i}.000000"
        t.first_message = f"thread number {i}"
        threads.append(t)
    poller = _make_mock_poller(threads)
    create_routes(app, poller, MockLlm(), _CONFIG)
    return TestClient(app)


def test_threads_has_no_row_cap() -> None:
    # Disclosure contract: even in compact mode (the default) every ranked thread is
    # server-rendered into the DOM - nothing is dropped server-side. Compact only hides
    # the below-fold tail via CSS, so all 40 thread bodies are present in the HTML.
    client = _client_with_n_threads(40)
    response = client.get("/threads")
    assert "thread number 39" in response.text
    assert "thread number 14" in response.text
    # Every thread renders a row regardless of the fold (zero-miss).
    assert response.text.count('class="thread-row') == 40


def test_threads_compact_tags_below_fold_tail() -> None:
    # With the default fold (compact_rows=20) and 40 threads, exactly the 20 past the fold
    # carry the below-fold class; the top 20 do not.
    client = _client_with_n_threads(40)
    response = client.get("/threads")
    assert response.text.count('class="thread-row below-fold"') == 20
    assert 'class="disclosure compact"' in response.text


def test_threads_full_mode_renders_no_compact_class() -> None:
    # compact=false flips to show-all: the wrapper drops the compact class so CSS reveals
    # the whole set, and the toggle offers to collapse back to the top N.
    client = _client_with_n_threads(40)
    response = client.get("/threads", params={"compact": "false"})
    assert 'class="disclosure"' in response.text
    assert 'class="disclosure compact"' not in response.text
    assert "show top 20 - collapse" in response.text


def test_threads_toggle_shows_hidden_count() -> None:
    # The toggle always carries a visible count of how much sits below the fold.
    client = _client_with_n_threads(40)
    response = client.get("/threads")
    assert "+20 more - show all 40" in response.text


def test_threads_no_toggle_when_under_fold(client: TestClient) -> None:
    # The single-thread fixture is well under the fold: nothing to disclose, no toggle,
    # and no row is tagged below the fold.
    response = client.get("/threads")
    assert "compact-toggle" not in response.text
    assert "below-fold" not in response.text


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_summarize_success(client: TestClient) -> None:
    response = client.get("/summarize/C123/1234567890.123456")
    assert response.status_code == 200
    assert "Mock summary" in response.text


def test_summarize_cached(client: TestClient) -> None:
    # First call generates summary
    client.get("/summarize/C123/1234567890.123456")
    # Second call should use cached
    response = client.get("/summarize/C123/1234567890.123456")
    assert response.status_code == 200
    assert "Mock summary" in response.text


def test_summarize_not_found(client: TestClient) -> None:
    response = client.get("/summarize/C999/9999999999.999999")
    assert response.status_code == 200
    assert "Failed" in response.text or "Retry" in response.text


def test_summarize_llm_failure() -> None:
    app = FastAPI()
    poller = _make_mock_poller()
    thread = _make_thread()
    poller.ranked_threads.return_value = [thread]
    poller.threads = {("C123", "1234567890.123456"): thread}
    create_routes(app, poller, FailingLlm(), _CONFIG)
    client = TestClient(app)
    response = client.get("/summarize/C123/1234567890.123456")
    assert response.status_code == 200
    assert "Retry" in response.text


def test_dismiss_route_invokes_dismiss_thread() -> None:
    app = FastAPI()
    poller = _make_mock_poller()
    thread = _make_thread()
    poller.ranked_threads.return_value = [thread]
    poller.threads = {("C123", "1234567890.123456"): thread}
    create_routes(app, poller, MockLlm(), _CONFIG)
    client = TestClient(app)
    response = client.post("/dismiss/C123/1234567890.123456")
    assert response.status_code == 200
    poller.dismiss_thread.assert_called_once_with("C123", "1234567890.123456")


def test_status_banner_disconnected() -> None:
    from slack_dashboard.connection import ConnectionState

    app = FastAPI()
    poller = _make_mock_poller([])
    poller.threads = {}
    conn = ConnectionState(socket_enabled=True, connected=False)
    create_routes(app, poller, MockLlm(), _CONFIG, conn)
    client = TestClient(app)
    resp = client.get("/status")
    assert resp.status_code == 200
    assert "Live connection lost" in resp.text


def test_status_banner_connected_is_empty() -> None:
    from slack_dashboard.connection import ConnectionState

    app = FastAPI()
    poller = _make_mock_poller([])
    poller.threads = {}
    conn = ConnectionState(socket_enabled=True, connected=True)
    create_routes(app, poller, MockLlm(), _CONFIG, conn)
    client = TestClient(app)
    resp = client.get("/status")
    assert resp.status_code == 200
    assert "Live connection lost" not in resp.text
    assert "Socket Mode is off" not in resp.text


def test_status_banner_disabled() -> None:
    from slack_dashboard.connection import ConnectionState

    app = FastAPI()
    poller = _make_mock_poller([])
    poller.threads = {}
    conn = ConnectionState(socket_enabled=False)
    create_routes(app, poller, MockLlm(), _CONFIG, conn)
    client = TestClient(app)
    resp = client.get("/status")
    assert resp.status_code == 200
    assert "Socket Mode is off" in resp.text


# ---------------------------------------------------------------------------
# Phase 1: Spiking glyph tests
# ---------------------------------------------------------------------------


def _make_spiking_thread(replies_in_window: int, heat_tier: str = "cold") -> ThreadEntry:
    """Thread with the given number of recent replies in the velocity window."""
    from slack_dashboard.thread import ReplyRecord

    now = _time.time()
    t = ThreadEntry(
        channel_id="C123",
        channel_name="sre",
        thread_ts="1234567890.123456",
        first_message="Something is happening",
        started_by="U1",
        message_count=replies_in_window,
        participants={"U1": 1},
        last_activity=datetime.now(UTC),
        heat_tier=heat_tier,
    )
    # Place all reply timestamps within the last minute so they are in the window.
    t.replies = [
        ReplyRecord(ts=now - i, author_id="U1", text="", is_root=(i == 0))
        for i in range(replies_in_window)
    ]
    return t


def test_spiking_glyph_fires_at_threshold() -> None:
    config = AppConfig(heat=HeatConfig(spiking_threshold=15, velocity_window_minutes=30))
    thread = _make_spiking_thread(replies_in_window=15)
    emojis = _emojis(thread, config, _NOW, _FAR_PAST_APP_START)
    assert "\N{HIGH VOLTAGE SIGN}" in emojis


def test_spiking_glyph_fires_above_threshold() -> None:
    config = AppConfig(heat=HeatConfig(spiking_threshold=15, velocity_window_minutes=30))
    thread = _make_spiking_thread(replies_in_window=20)
    emojis = _emojis(thread, config, _NOW, _FAR_PAST_APP_START)
    assert "\N{HIGH VOLTAGE SIGN}" in emojis


def test_spiking_glyph_absent_below_threshold() -> None:
    config = AppConfig(heat=HeatConfig(spiking_threshold=15, velocity_window_minutes=30))
    thread = _make_spiking_thread(replies_in_window=14)
    emojis = _emojis(thread, config, _NOW, _FAR_PAST_APP_START)
    assert "\N{HIGH VOLTAGE SIGN}" not in emojis


def test_spiking_glyph_absent_with_zero_replies() -> None:
    config = AppConfig(heat=HeatConfig(spiking_threshold=15, velocity_window_minutes=30))
    thread = _make_spiking_thread(replies_in_window=0)
    emojis = _emojis(thread, config, _NOW, _FAR_PAST_APP_START)
    assert "\N{HIGH VOLTAGE SIGN}" not in emojis


def test_spiking_glyph_distinct_from_fire() -> None:
    # A spiking thread that is NOT hot should show ⚡ but not 🔥.
    config = AppConfig(heat=HeatConfig(spiking_threshold=15, velocity_window_minutes=30))
    thread = _make_spiking_thread(replies_in_window=20, heat_tier="cold")
    emojis = _emojis(thread, config, _NOW, _FAR_PAST_APP_START)
    assert "\N{HIGH VOLTAGE SIGN}" in emojis
    assert "\N{FIRE}" not in emojis


def test_spiking_and_fire_can_coexist() -> None:
    # A thread can be both spiking and hot; both glyphs should appear.
    config = AppConfig(heat=HeatConfig(spiking_threshold=15, velocity_window_minutes=30))
    thread = _make_spiking_thread(replies_in_window=20, heat_tier="hot")
    emojis = _emojis(thread, config, _NOW, _FAR_PAST_APP_START)
    assert "\N{HIGH VOLTAGE SIGN}" in emojis
    assert "\N{FIRE}" in emojis


def test_spiking_glyph_precedes_fire_in_render_order() -> None:
    # Glyph order: new, vip, spiking, fire, zombie. Spiking must appear before fire.
    config = AppConfig(heat=HeatConfig(spiking_threshold=15, velocity_window_minutes=30))
    thread = _make_spiking_thread(replies_in_window=20, heat_tier="hot")
    emojis = _emojis(thread, config, _NOW, _FAR_PAST_APP_START)
    spiking_pos = emojis.index("\N{HIGH VOLTAGE SIGN}")
    fire_pos = emojis.index("\N{FIRE}")
    assert spiking_pos < fire_pos


# ---------------------------------------------------------------------------
# Phase 3: New glyph tests
# ---------------------------------------------------------------------------


def _make_new_thread(
    first_observed_at: float,
    heat_tier: str = "cold",
    resurrection_event_ts: float = 0.0,
    first_seen_ts: float = 0.0,
) -> ThreadEntry:
    """Thread with the given first_observed_at for testing the new glyph."""
    return ThreadEntry(
        channel_id="C123",
        channel_name="sre",
        thread_ts="1234567890.123456",
        first_message="Brand new thread",
        started_by="U1",
        message_count=5,
        participants={"U1": 3},
        last_activity=datetime.now(UTC),
        heat_tier=heat_tier,
        first_observed_at=first_observed_at,
        resurrection_event_ts=resurrection_event_ts,
        first_seen_ts=first_seen_ts,
    )


def test_new_glyph_fires_inside_window() -> None:
    # first_observed 30 min ago, window 60 min, app started 2 hours ago - glyph fires.
    now = _time.time()
    app_start_at = now - 7200  # 2 hours ago - storm suppressor clear
    first_observed_at = now - 1800  # 30 min ago - inside 60-min window
    config = AppConfig(heat=HeatConfig(new_window_minutes=60))
    thread = _make_new_thread(first_observed_at=first_observed_at)
    emojis = _emojis(thread, config, now, app_start_at)
    assert "\N{SPARKLES}" in emojis


def test_new_glyph_absent_outside_window() -> None:
    # first_observed 90 min ago, window 60 min - glyph absent.
    now = _time.time()
    app_start_at = now - 7200  # storm suppressor clear
    first_observed_at = now - 5400  # 90 min ago - outside 60-min window
    config = AppConfig(heat=HeatConfig(new_window_minutes=60))
    thread = _make_new_thread(first_observed_at=first_observed_at)
    emojis = _emojis(thread, config, now, app_start_at)
    assert "\N{SPARKLES}" not in emojis


def test_new_glyph_absent_when_first_observed_zero() -> None:
    # first_observed_at == 0 means degraded/unknown - glyph must not fire.
    now = _time.time()
    app_start_at = now - 7200
    config = AppConfig(heat=HeatConfig(new_window_minutes=60))
    thread = _make_new_thread(first_observed_at=0.0)
    emojis = _emojis(thread, config, now, app_start_at)
    assert "\N{SPARKLES}" not in emojis


def test_new_glyph_absent_when_zombie() -> None:
    # Thread inside the new window but is a zombie - glyph must not fire (B2 guard).
    now = _time.time()
    app_start_at = now - 7200
    first_observed_at = now - 1800  # 30 min ago - inside window
    # Make it a zombie: resurrection_event_ts recent, first_seen_ts old
    resurrection_event_ts = now - 3600  # 1 hour ago - within display window (24h default)
    first_seen_ts = now - (3 * 86400)  # 3 days ago - older than resurrection_age_days=2
    config = AppConfig(heat=HeatConfig(new_window_minutes=60))
    thread = _make_new_thread(
        first_observed_at=first_observed_at,
        resurrection_event_ts=resurrection_event_ts,
        first_seen_ts=first_seen_ts,
    )
    emojis = _emojis(thread, config, now, app_start_at)
    assert "\N{SPARKLES}" not in emojis
    assert "\N{ZOMBIE}" in emojis


def test_new_glyph_absent_within_app_start_window() -> None:
    # App started 30 min ago, window is 60 min - storm suppressor active, glyph absent.
    now = _time.time()
    app_start_at = now - 1800  # 30 min ago - within 60-min suppressor window
    first_observed_at = now - 60  # 1 min ago - definitely inside observation window
    config = AppConfig(heat=HeatConfig(new_window_minutes=60))
    thread = _make_new_thread(first_observed_at=first_observed_at)
    emojis = _emojis(thread, config, now, app_start_at)
    assert "\N{SPARKLES}" not in emojis


def test_new_glyph_storm_suppressor_lifts_after_window() -> None:
    # App started exactly new_window_minutes ago - suppressor just expired, glyph fires.
    now = _time.time()
    new_window_minutes = 60
    new_window = new_window_minutes * 60
    app_start_at = now - new_window  # exactly at the boundary - suppressor done
    first_observed_at = now - 1800  # 30 min ago - inside observation window
    config = AppConfig(heat=HeatConfig(new_window_minutes=new_window_minutes))
    thread = _make_new_thread(first_observed_at=first_observed_at)
    emojis = _emojis(thread, config, now, app_start_at)
    assert "\N{SPARKLES}" in emojis


def test_new_glyph_precedes_vip_in_render_order() -> None:
    # Glyph order: new, vip, spiking, fire, zombie. New must lead.
    now = _time.time()
    app_start_at = now - 7200
    first_observed_at = now - 1800
    # Set up a VIP thread: use a people_weights config
    config = AppConfig(
        heat=HeatConfig(
            new_window_minutes=60,
            people_weights={"U1": 10.0},
            participant_weight=3,
        )
    )
    thread = _make_new_thread(first_observed_at=first_observed_at)
    emojis = _emojis(thread, config, now, app_start_at)
    assert "\N{SPARKLES}" in emojis
    assert "\N{CROWN}" in emojis
    new_pos = emojis.index("\N{SPARKLES}")
    vip_pos = emojis.index("\N{CROWN}")
    assert new_pos < vip_pos


def test_new_glyph_suppressed_for_all_threads_in_storm_window() -> None:
    # All threads within new_window of app_start must not show new, regardless of
    # their first_observed_at. Simulates the app-start storm suppressor (M2).
    now = _time.time()
    new_window_minutes = 60
    new_window = new_window_minutes * 60
    app_start_at = now - (new_window / 2)  # 30 min after start - suppressor still active
    config = AppConfig(heat=HeatConfig(new_window_minutes=new_window_minutes))
    # Several threads with varying first_observed_at all inside the observation window
    threads = [
        _make_new_thread(first_observed_at=now - 60),  # 1 min ago
        _make_new_thread(first_observed_at=now - 600),  # 10 min ago
        _make_new_thread(first_observed_at=now - 1800),  # 30 min ago
    ]
    for t in threads:
        emojis = _emojis(t, config, now, app_start_at)
        assert "\N{SPARKLES}" not in emojis, (
            f"storm suppressor should block new glyph; first_observed_at={t.first_observed_at}"
        )


# ---------------------------------------------------------------------------
# Phase 4: Unanswered proxy glyph tests
# ---------------------------------------------------------------------------


def _make_unanswered_thread(
    first_message: str = "Is this still broken?",
    reply_count: int = 1,
    age_seconds: float = 7300.0,  # just over 2 hours by default (avoids fp boundary)
    base_time: float | None = None,
) -> ThreadEntry:
    """Thread aged by age_seconds relative to base_time (or now if unset).

    Pass base_time=now when the test also calls _emojis(thread, config, now, ...) so
    the age computation uses a consistent epoch: thread_age = now - thread_ts ~= age_seconds.
    Use age_seconds slightly above the threshold (e.g. 7300 instead of 7200) to avoid
    floating-point truncation from the 6-decimal thread_ts format causing an off-by-epsilon
    miss at the exact boundary.
    """
    ts = base_time if base_time is not None else _time.time()
    # thread_ts encodes thread creation time; age it back by age_seconds
    thread_ts_epoch = ts - age_seconds
    thread_ts = f"{thread_ts_epoch:.6f}"
    return ThreadEntry(
        channel_id="C123",
        channel_name="ask-security",
        thread_ts=thread_ts,
        first_message=first_message,
        started_by="U1",
        message_count=reply_count,
        participants={"U1": 1},
        last_activity=datetime.now(UTC),
        heat_tier="cold",
    )


def _unanswered_config(
    enabled: bool = True,
    max_replies: int = 2,
    min_age_hours: int = 2,
) -> AppConfig:
    return AppConfig(
        heat=HeatConfig(
            unanswered_enabled=enabled,
            unanswered_max_replies=max_replies,
            unanswered_min_age_hours=min_age_hours,
        )
    )


def test_unanswered_glyph_fires_when_enabled_question_low_replies_aged() -> None:
    # All four conditions met: enabled, "?" in message, low reply count, aged enough.
    now = _time.time()
    thread = _make_unanswered_thread(
        first_message="Can you look at this?", reply_count=1, age_seconds=7300, base_time=now
    )
    config = _unanswered_config(enabled=True, max_replies=2, min_age_hours=2)
    emojis = _emojis(thread, config, now, _FAR_PAST_APP_START)
    assert "\N{BLACK QUESTION MARK ORNAMENT}" in emojis


def test_unanswered_glyph_absent_when_disabled_by_default() -> None:
    # Default config has unanswered_enabled=False - glyph must not fire.
    now = _time.time()
    thread = _make_unanswered_thread(
        first_message="Can you look at this?", reply_count=1, age_seconds=7300, base_time=now
    )
    config = AppConfig()  # default config - unanswered_enabled=False
    emojis = _emojis(thread, config, now, _FAR_PAST_APP_START)
    assert "\N{BLACK QUESTION MARK ORNAMENT}" not in emojis


def test_unanswered_glyph_absent_when_no_question_mark() -> None:
    # Enabled, aged, low replies, but no "?" in first_message - glyph absent.
    now = _time.time()
    thread = _make_unanswered_thread(
        first_message="Something broke in prod", reply_count=1, age_seconds=7300, base_time=now
    )
    config = _unanswered_config(enabled=True, max_replies=2, min_age_hours=2)
    emojis = _emojis(thread, config, now, _FAR_PAST_APP_START)
    assert "\N{BLACK QUESTION MARK ORNAMENT}" not in emojis


def test_unanswered_glyph_absent_when_reply_count_exceeds_max() -> None:
    # reply_count is above the max - glyph absent.
    now = _time.time()
    thread = _make_unanswered_thread(
        first_message="Is this still broken?", reply_count=3, age_seconds=7300, base_time=now
    )
    config = _unanswered_config(enabled=True, max_replies=2, min_age_hours=2)
    emojis = _emojis(thread, config, now, _FAR_PAST_APP_START)
    assert "\N{BLACK QUESTION MARK ORNAMENT}" not in emojis


def test_unanswered_glyph_fires_at_exact_max_replies() -> None:
    # reply_count == max_replies (boundary: <= is inclusive) - glyph fires.
    now = _time.time()
    thread = _make_unanswered_thread(
        first_message="Is this still broken?", reply_count=2, age_seconds=7300, base_time=now
    )
    config = _unanswered_config(enabled=True, max_replies=2, min_age_hours=2)
    emojis = _emojis(thread, config, now, _FAR_PAST_APP_START)
    assert "\N{BLACK QUESTION MARK ORNAMENT}" in emojis


def test_unanswered_glyph_absent_when_too_young() -> None:
    # Thread is only 1 hour old, min_age is 2 hours - glyph absent.
    now = _time.time()
    thread = _make_unanswered_thread(
        first_message="Is this still broken?",
        reply_count=1,
        age_seconds=3600,  # 1 hour - well below the 2-hour floor
        base_time=now,
    )
    config = _unanswered_config(enabled=True, max_replies=2, min_age_hours=2)
    emojis = _emojis(thread, config, now, _FAR_PAST_APP_START)
    assert "\N{BLACK QUESTION MARK ORNAMENT}" not in emojis


def test_unanswered_glyph_fires_with_question_mark_inside_message() -> None:
    # "?" appears mid-message (not just at the end) - contains check, not ends-with.
    now = _time.time()
    thread = _make_unanswered_thread(
        first_message="Wondering if this is the right approach? Let me know.",
        reply_count=0,
        age_seconds=7300,
        base_time=now,
    )
    config = _unanswered_config(enabled=True, max_replies=2, min_age_hours=2)
    emojis = _emojis(thread, config, now, _FAR_PAST_APP_START)
    assert "\N{BLACK QUESTION MARK ORNAMENT}" in emojis


def test_unanswered_glyph_leads_before_new_glyph() -> None:
    # When unanswered fires alongside new, unanswered must appear first (leads).
    now = _time.time()
    first_observed_at = now - 1800  # 30 min ago - inside the new window
    app_start_at = now - 7200  # storm suppressor clear
    thread = _make_unanswered_thread(
        first_message="Is this fixed?", reply_count=1, age_seconds=7300, base_time=now
    )
    thread.first_observed_at = first_observed_at
    config = AppConfig(
        heat=HeatConfig(
            unanswered_enabled=True,
            unanswered_max_replies=2,
            unanswered_min_age_hours=2,
            new_window_minutes=60,
        )
    )
    emojis = _emojis(thread, config, now, app_start_at)
    assert "\N{BLACK QUESTION MARK ORNAMENT}" in emojis
    assert "\N{SPARKLES}" in emojis
    unanswered_pos = emojis.index("\N{BLACK QUESTION MARK ORNAMENT}")
    new_pos = emojis.index("\N{SPARKLES}")
    assert unanswered_pos < new_pos
