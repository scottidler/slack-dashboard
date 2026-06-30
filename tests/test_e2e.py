import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from slack_dashboard.config import AppConfig, FetchConfig, HeatConfig
from slack_dashboard.dismiss import DismissStore
from slack_dashboard.llm.provider import LlmProvider, SummaryResult
from slack_dashboard.slack.client import SlackClient
from slack_dashboard.slack.poller import SlackPoller
from slack_dashboard.slack.queue import PRIORITY_BACKFILL, FetchItem
from slack_dashboard.web import create_routes

_NOW = str(time.time())
_R1 = str(time.time() + 1)
_R2 = str(time.time() + 2)


class _Llm(LlmProvider):
    async def generate_title(self, messages: list[str]) -> str | None:
        return "Title"

    async def generate_summary(self, messages: list[str]) -> SummaryResult:
        return SummaryResult(bullets="Summary", tone=0)


def _mock_slack() -> AsyncMock:
    client = AsyncMock(spec=SlackClient)
    client.resolve_user = AsyncMock(side_effect=lambda uid: uid)
    client.fetch_threads = AsyncMock(
        return_value=[{"ts": _NOW, "text": "prod is down", "reply_count": 2, "thread_ts": _NOW}]
    )
    client.fetch_replies = AsyncMock(
        return_value=[
            {"ts": _NOW, "user": "U1", "text": "prod is down"},
            {"ts": _R1, "user": "U2", "text": "same in us-east"},
            {"ts": _R2, "user": "U3", "text": "tied to deploy X"},
        ]
    )
    return client


@pytest.mark.asyncio
async def test_e2e_render_dismiss_and_persist_across_restart(tmp_path: Path) -> None:
    dismiss_path = tmp_path / "dismissed.jsonl"
    config = AppConfig(
        channels={"incidents": "C111"},
        workspace="tatari",
        heat=HeatConfig(channel_weights={"incidents": 2.0}),
        fetch=FetchConfig(channel_min_replies={"incidents": 1}),
    )

    store = DismissStore(dismiss_path)
    store.load()
    poller = SlackPoller(_mock_slack(), config, dismiss=store)
    await poller._process_item(
        FetchItem(priority=PRIORITY_BACKFILL, channel_id="C111", channel_name="incidents")
    )

    app = FastAPI()
    create_routes(app, poller, _Llm(), config)
    client = TestClient(app)

    # The thread renders as a compact row with a working deep link
    resp = client.get("/threads")
    assert resp.status_code == 200
    assert "incidents" in resp.text
    assert "prod is down" in resp.text
    assert f"https://tatari.slack.com/archives/C111/p{_NOW.replace('.', '')}" in resp.text

    # Dismiss it
    resp = client.post(f"/dismiss/C111/{_NOW}")
    assert resp.status_code == 200
    assert client.get("/threads").text.find("prod is down") == -1

    # Simulate a restart: a fresh store + poller loading the same dismiss file
    store2 = DismissStore(dismiss_path)
    store2.load()
    assert store2.is_dismissed("C111", _NOW)
    poller2 = SlackPoller(_mock_slack(), config, dismiss=store2)
    # The dismissed thread is short-circuited before any REST fetch on backfill
    await poller2._process_item(
        FetchItem(priority=PRIORITY_BACKFILL, channel_id="C111", channel_name="incidents")
    )
    assert ("C111", _NOW) not in poller2.threads
    assert poller2.ranked_threads() == []


@pytest.mark.asyncio
async def test_reconnect_recovers_reply_missed_during_disconnect() -> None:
    """End-to-end stand-in for live Socket Mode (which can only be exercised during work
    hours): a reply that lands on an OLD parent while disconnected is recovered on reconnect,
    driven through the real ConnectionState + monitor + poller.reconcile + _fetch_thread."""
    import asyncio
    import contextlib

    from slack_dashboard.connection import ConnectionState, monitor_connection

    now = time.time()
    parent = str(now - 3600)
    r1 = str(now - 1800)
    r2_missed = str(now - 30)  # arrives during the disconnect window

    mock_slack = AsyncMock(spec=SlackClient)
    mock_slack.resolve_user = AsyncMock(side_effect=lambda uid: uid)
    # Phase A - backfill sees the parent + one reply
    mock_slack.fetch_threads = AsyncMock(
        return_value=[{"ts": parent, "thread_ts": parent, "reply_count": 1, "latest_reply": r1}]
    )
    mock_slack.fetch_replies = AsyncMock(
        return_value=[
            {"ts": parent, "user": "U1", "text": "root"},
            {"ts": r1, "user": "U2", "text": "first"},
        ]
    )
    config = AppConfig(channels={"sre": "C1"})
    poller = SlackPoller(mock_slack, config)
    poller._channel_map = config.channels
    await poller._fetch_channel("C1", "sre")
    key = ("C1", parent)
    assert poller.thread_watermarks[key] == r1
    replies_before = poller.threads[key].message_count

    # The connection drops (on_close edge): banner shows disconnected, reconcile armed
    conn = ConnectionState(socket_enabled=True, connected=True)
    conn.mark_disconnected()
    assert conn.status() == "disconnected"

    # While disconnected, a new reply lands on the OLD parent - Socket Mode never delivered it
    mock_slack.fetch_threads = AsyncMock(
        return_value=[
            {"ts": parent, "thread_ts": parent, "reply_count": 2, "latest_reply": r2_missed}
        ]
    )
    mock_slack.fetch_replies = AsyncMock(
        return_value=[
            {"ts": parent, "user": "U1", "text": "root"},
            {"ts": r1, "user": "U2", "text": "first"},
            {"ts": r2_missed, "user": "U3", "text": "landed during the outage"},
        ]
    )

    # Reconnect: the monitor observes connected with a pending reconcile and trues up the gap
    seq = iter([True])

    async def is_connected() -> bool:
        return next(seq, True)

    task = asyncio.create_task(
        monitor_connection(is_connected, conn, poller.reconcile, interval=0.01)
    )
    await asyncio.sleep(0.08)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert conn.status() == "connected"  # banner cleared
    assert poller.thread_watermarks[key] == r2_missed  # watermark advanced past the gap
    assert poller.threads[key].message_count > replies_before  # the missed reply was recovered
