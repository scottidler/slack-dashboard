import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from anthropic import AsyncAnthropic
from fastapi import FastAPI

from slack_dashboard.config import AppConfig, load_config
from slack_dashboard.connection import ConnectionState
from slack_dashboard.dismiss import DismissStore
from slack_dashboard.llm.provider import AnthropicProvider
from slack_dashboard.slack.client import SlackClient, create_slack_client
from slack_dashboard.slack.listener import SocketListener
from slack_dashboard.slack.mrkdwn import strip_mrkdwn
from slack_dashboard.slack.poller import SlackPoller
from slack_dashboard.thread import ThreadEntry
from slack_dashboard.web import create_routes

logger = logging.getLogger(__name__)

_XDG_CONFIG_HOME = Path.home() / ".config"
_CONFIG_PATH = _XDG_CONFIG_HOME / "slack-dashboard" / "slack-dashboard.yml"


def _resolve_config_path() -> Path:
    import os

    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg:
        return Path(xdg) / "slack-dashboard" / "slack-dashboard.yml"
    return _CONFIG_PATH


def _resolve_dismiss_path() -> Path:
    # The permanent dismiss record lives alongside the config.
    return _resolve_config_path().parent / "dismissed.jsonl"


def _build_app(config: AppConfig) -> tuple[FastAPI, SlackPoller]:
    slack_web_client = create_slack_client(config.slack.token)
    slack_client = SlackClient(slack_web_client)
    anthropic_client = AsyncAnthropic(api_key=config.llm.api_key)
    llm = AnthropicProvider(anthropic_client, model=config.llm.model)

    async def on_title_needed(entry: ThreadEntry, reply_texts: list[str]) -> None:
        messages = [strip_mrkdwn(t) for t in reply_texts]
        title = await llm.generate_title(messages)
        if title:
            entry.title = title
            entry.title_watermark = entry.reply_count

    async def on_summary_needed(entry: ThreadEntry, reply_texts: list[str]) -> None:
        messages = [strip_mrkdwn(t) for t in reply_texts]
        summary = await llm.generate_summary(messages)
        if summary:
            entry.summary = summary
            entry.summary_watermark = entry.reply_count

    dismiss_store = DismissStore(_resolve_dismiss_path())
    dismiss_store.load()
    poller = SlackPoller(
        slack_client,
        config,
        on_title_needed=on_title_needed,
        on_summary_needed=on_summary_needed,
        dismiss=dismiss_store,
    )

    channel_names = {v: k for k, v in config.channels.items()}
    socket_listener = SocketListener(
        queue=poller.queue,
        threads=poller.threads,
        channel_ids=set(config.channels.values()),
        channel_names=channel_names,
        heat_config=config.heat,
    )

    connection = ConnectionState(socket_enabled=bool(config.slack.app_token))

    async def _on_close(_message: Any) -> None:
        # Disconnect edge: flip the banner immediately (the monitor below catches reconnect).
        connection.connected = False
        logger.warning("Socket Mode connection closed; trust banner raised")

    async def _connection_monitor(socket_client: Any) -> None:
        # slack_sdk exposes on_close_listeners (disconnect) and is_connected(), but no
        # on-connect callback, so we poll is_connected() to detect the reconnect edge and
        # reconcile the gap. Auto-reconnect is the SDK's job; truing up missed replies is ours.
        was_connected = True
        try:
            while True:
                await asyncio.sleep(5)
                connected = bool(socket_client.is_connected())
                connection.connected = connected
                if connected and not was_connected:
                    logger.info("Socket Mode reconnected; reconciling missed activity")
                    try:
                        await poller.reconcile()
                    except Exception:
                        logger.exception("reconcile after reconnect failed")
                was_connected = connected
        except asyncio.CancelledError:
            return

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("Starting Slack fetcher...")
        await poller.start()
        socket_client = None
        monitor_task: asyncio.Task[None] | None = None
        if config.slack.app_token:
            from slack_sdk.socket_mode.aiohttp import SocketModeClient

            socket_client = SocketModeClient(
                app_token=config.slack.app_token,
                web_client=slack_web_client,
                on_close_listeners=[_on_close],
            )
            socket_client.socket_mode_request_listeners.append(socket_listener.handle_event)
            logger.info("Starting Socket Mode listener...")
            await socket_client.connect()  # type: ignore[no-untyped-call]
            connection.connected = True
            monitor_task = asyncio.create_task(_connection_monitor(socket_client))
        yield
        if monitor_task:
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task
        if socket_client:
            logger.info("Stopping Socket Mode listener...")
            await socket_client.close()  # type: ignore[no-untyped-call]
        logger.info("Stopping Slack fetcher...")
        await poller.stop()

    app = FastAPI(lifespan=lifespan)
    create_routes(app, poller, llm, config, connection)
    return app, poller


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    config_path = _resolve_config_path()
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path)
        raise SystemExit(1)
    config = load_config(config_path)
    logging.basicConfig(
        level=getattr(logging, config.server.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    app, _ = _build_app(config)
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        log_level=config.server.log_level,
    )


if __name__ == "__main__":
    main()
