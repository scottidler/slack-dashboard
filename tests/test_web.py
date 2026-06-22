from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from slack_dashboard.config import AppConfig
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
