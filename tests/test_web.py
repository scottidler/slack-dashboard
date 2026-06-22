from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from slack_dashboard.config import AppConfig, SlackConfig
from slack_dashboard.llm.provider import LlmProvider
from slack_dashboard.slack.poller import SlackPoller
from slack_dashboard.thread import ThreadEntry
from slack_dashboard.web import create_routes

_CONFIG = AppConfig(workspace="tatari")


class MockLlm(LlmProvider):
    async def generate_title(self, messages: list[str]) -> str | None:
        return "Mock Title"

    async def generate_summary(self, messages: list[str]) -> str | None:
        return "Mock summary of the thread."


class FailingLlm(LlmProvider):
    async def generate_title(self, messages: list[str]) -> str | None:
        return None

    async def generate_summary(self, messages: list[str]) -> str | None:
        return None


def _make_thread() -> ThreadEntry:
    return ThreadEntry(
        channel_id="C123",
        channel_name="sre-internal",
        thread_ts="1234567890.123456",
        first_message="Something broke in prod",
        started_by="U1",
        reply_count=10,
        participants={"U1": 3, "U2": 2, "U3": 1},
        last_activity=datetime(2026, 3, 24, 12, 0, 0, tzinfo=UTC),
        heat_score=80.0,
        heat_tier="hot",
    )


@pytest.fixture
def app_with_threads() -> FastAPI:
    app = FastAPI()
    poller = AsyncMock(spec=SlackPoller)
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
    # Compact counts: "10r" replies, "3p" participants
    assert "10r" in response.text
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
    poller = AsyncMock(spec=SlackPoller)
    poller.ranked_threads.return_value = [_make_thread()]
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
    poller = AsyncMock(spec=SlackPoller)
    poller.ranked_threads.return_value = [_make_thread()]
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
    poller = AsyncMock(spec=SlackPoller)
    poller.ranked_threads.return_value = [_make_thread()]
    config = AppConfig(slack=SlackConfig(team_id="T999"))
    create_routes(app, poller, MockLlm(), config)
    response = TestClient(app).get("/threads")
    # Channel handle opens the channel root in the desktop app (no &message=).
    assert 'href="slack://channel?team=T999&amp;id=C123"' in response.text


def test_threads_renders_fire_emoji_for_hot(client: TestClient) -> None:
    # The fixture thread is heat_tier="hot"
    response = client.get("/threads")
    assert "\N{FIRE}" in response.text


def test_threads_has_no_row_cap() -> None:
    app = FastAPI()
    poller = AsyncMock(spec=SlackPoller)
    threads = []
    for i in range(40):
        t = _make_thread()
        t.thread_ts = f"100000000{i}.000000"
        t.first_message = f"thread number {i}"
        threads.append(t)
    poller.ranked_threads.return_value = threads
    create_routes(app, poller, MockLlm(), _CONFIG)
    client = TestClient(app)
    response = client.get("/threads")
    # All 40 render - the old threads[:15] cap is gone (zero-miss invariant)
    assert "thread number 39" in response.text
    assert "thread number 14" in response.text


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
    poller = AsyncMock(spec=SlackPoller)
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
    poller = AsyncMock(spec=SlackPoller)
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
    poller = AsyncMock(spec=SlackPoller)
    poller.ranked_threads.return_value = []
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
    poller = AsyncMock(spec=SlackPoller)
    poller.ranked_threads.return_value = []
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
    poller = AsyncMock(spec=SlackPoller)
    poller.ranked_threads.return_value = []
    poller.threads = {}
    conn = ConnectionState(socket_enabled=False)
    create_routes(app, poller, MockLlm(), _CONFIG, conn)
    client = TestClient(app)
    resp = client.get("/status")
    assert resp.status_code == 200
    assert "Socket Mode is off" in resp.text
