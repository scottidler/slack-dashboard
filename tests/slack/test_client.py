from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from slack_dashboard.slack.client import SlackClient


def _mock_response(data: dict[str, Any]) -> MagicMock:
    resp = MagicMock()
    resp.data = data
    return resp


@pytest.fixture
def mock_slack() -> AsyncMock:
    client = AsyncMock()
    client.conversations_list = AsyncMock(
        return_value=_mock_response(
            {
                "ok": True,
                "channels": [
                    {"id": "C111", "name": "sre-internal"},
                    {"id": "C222", "name": "data-platform"},
                    {"id": "C333", "name": "random"},
                ],
                "response_metadata": {"next_cursor": ""},
            }
        )
    )
    return client


@pytest.mark.asyncio
async def test_resolve_channels(mock_slack: AsyncMock) -> None:
    client = SlackClient(mock_slack)
    channel_map = await client.resolve_channels(["sre-internal", "data-platform"])
    assert channel_map == {"sre-internal": "C111", "data-platform": "C222"}


@pytest.mark.asyncio
async def test_resolve_channels_missing_warns(
    mock_slack: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    client = SlackClient(mock_slack)
    channel_map = await client.resolve_channels(["sre-internal", "nonexistent"])
    assert "sre-internal" in channel_map
    assert "nonexistent" not in channel_map
    assert "not found" in caplog.text.lower()


@pytest.mark.asyncio
async def test_fetch_threads(mock_slack: AsyncMock) -> None:
    mock_slack.conversations_history = AsyncMock(
        return_value=_mock_response(
            {
                "ok": True,
                "messages": [
                    {"ts": "1.1", "text": "Thread msg", "reply_count": 5, "thread_ts": "1.1"},
                    {"ts": "2.2", "text": "Single msg"},
                    {"ts": "3.3", "text": "Another thread", "reply_count": 4, "thread_ts": "3.3"},
                ],
                "has_more": False,
            }
        )
    )
    client = SlackClient(mock_slack)
    threads = await client.fetch_threads("C111")
    assert len(threads) == 2
    assert threads[0]["ts"] == "1.1"
    assert threads[1]["ts"] == "3.3"


@pytest.mark.asyncio
async def test_fetch_replies(mock_slack: AsyncMock) -> None:
    mock_slack.conversations_replies = AsyncMock(
        return_value=_mock_response(
            {
                "ok": True,
                "messages": [
                    {"ts": "1.1", "user": "U1", "text": "root"},
                    {"ts": "1.2", "user": "U2", "text": "reply 1"},
                    {"ts": "1.3", "user": "U1", "text": "reply 2"},
                    {"ts": "1.4", "user": "U3", "text": "reply 3"},
                ],
                "has_more": False,
            }
        )
    )
    client = SlackClient(mock_slack)
    replies = await client.fetch_replies("C111", "1.1")
    assert len(replies) == 4
    assert replies[1]["user"] == "U2"
