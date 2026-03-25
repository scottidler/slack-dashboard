import asyncio
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


@pytest.mark.asyncio
async def test_fetch_threads_page_size(mock_slack: AsyncMock) -> None:
    mock_slack.conversations_history = AsyncMock(
        return_value=_mock_response({"ok": True, "messages": [], "has_more": False})
    )
    client = SlackClient(mock_slack)
    await client.fetch_threads("C111")
    call_kwargs = mock_slack.conversations_history.call_args[1]
    assert call_kwargs["limit"] == 200


@pytest.mark.asyncio
async def test_fetch_replies_page_size(mock_slack: AsyncMock) -> None:
    mock_slack.conversations_replies = AsyncMock(
        return_value=_mock_response({"ok": True, "messages": [], "has_more": False})
    )
    client = SlackClient(mock_slack)
    await client.fetch_replies("C111", "1.1")
    call_kwargs = mock_slack.conversations_replies.call_args[1]
    assert call_kwargs["limit"] == 1000


@pytest.mark.asyncio
async def test_fetch_threads_with_oldest(mock_slack: AsyncMock) -> None:
    mock_slack.conversations_history = AsyncMock(
        return_value=_mock_response({"ok": True, "messages": [], "has_more": False})
    )
    client = SlackClient(mock_slack)
    await client.fetch_threads("C111", oldest="1234.5678")
    call_kwargs = mock_slack.conversations_history.call_args[1]
    assert call_kwargs["oldest"] == "1234.5678"


@pytest.mark.asyncio
async def test_fetch_replies_with_oldest(mock_slack: AsyncMock) -> None:
    mock_slack.conversations_replies = AsyncMock(
        return_value=_mock_response({"ok": True, "messages": [], "has_more": False})
    )
    client = SlackClient(mock_slack)
    await client.fetch_replies("C111", "1.1", oldest="1.2")
    call_kwargs = mock_slack.conversations_replies.call_args[1]
    assert call_kwargs["oldest"] == "1.2"


@pytest.mark.asyncio
async def test_history_and_replies_semaphores_independent(mock_slack: AsyncMock) -> None:
    """Verify history and replies can proceed concurrently via separate semaphores."""
    history_entered = asyncio.Event()
    replies_entered = asyncio.Event()

    async def blocking_history(**kwargs: Any) -> MagicMock:
        history_entered.set()
        await replies_entered.wait()
        return _mock_response({"ok": True, "messages": [], "has_more": False})

    async def blocking_replies(**kwargs: Any) -> MagicMock:
        replies_entered.set()
        await history_entered.wait()
        return _mock_response({"ok": True, "messages": [], "has_more": False})

    mock_slack.conversations_history = blocking_history
    mock_slack.conversations_replies = blocking_replies
    client = SlackClient(mock_slack)
    # Patch out the post-call sleep
    original_sleep = asyncio.sleep
    asyncio.sleep = AsyncMock(return_value=None)  # type: ignore[assignment]
    try:
        # If semaphores were shared, this would deadlock (each waits for the other).
        # With independent semaphores, both enter concurrently and unblock each other.
        await asyncio.wait_for(
            asyncio.gather(
                client.fetch_threads("C111"),
                client.fetch_replies("C111", "1.1"),
            ),
            timeout=2.0,
        )
    finally:
        asyncio.sleep = original_sleep  # type: ignore[assignment]
    assert history_entered.is_set()
    assert replies_entered.is_set()
