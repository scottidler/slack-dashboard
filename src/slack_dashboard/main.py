import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from anthropic import AsyncAnthropic
from fastapi import FastAPI

from slack_dashboard.config import AppConfig, load_config
from slack_dashboard.llm.provider import AnthropicProvider
from slack_dashboard.slack.client import SlackClient, create_slack_client
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

    poller = SlackPoller(
        slack_client,
        config,
        on_title_needed=on_title_needed,
        on_summary_needed=on_summary_needed,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("Starting Slack poller...")
        await poller.start()
        yield
        logger.info("Stopping Slack poller...")
        await poller.stop()

    app = FastAPI(lifespan=lifespan)
    create_routes(app, poller, llm)
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
