from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from slack_dashboard.llm.provider import LlmProvider
from slack_dashboard.slack.poller import SlackPoller
from slack_dashboard.thread import ThreadEntry
from slack_dashboard.web import create_routes


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
    create_routes(app, poller, MockLlm())
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
    assert "10 replies" in response.text


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
    create_routes(app, poller, FailingLlm())
    client = TestClient(app)
    response = client.get("/summarize/C123/1234567890.123456")
    assert response.status_code == 200
    assert "Retry" in response.text
