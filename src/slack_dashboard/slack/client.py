import asyncio
import logging
from typing import Any

from slack_sdk.http_retry.async_handler import AsyncRetryHandler
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
from slack_sdk.web.async_client import AsyncWebClient

logger = logging.getLogger(__name__)


def create_slack_client(token: str) -> AsyncWebClient:
    client = AsyncWebClient(token=token)
    handler: AsyncRetryHandler = AsyncRateLimitErrorRetryHandler(max_retry_count=3)
    client.retry_handlers.append(handler)
    return client


class SlackClient:
    def __init__(self, client: AsyncWebClient) -> None:
        self._client = client
        self._semaphore = asyncio.Semaphore(1)

    async def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        async with self._semaphore:
            func = getattr(self._client, method)
            response = await func(**kwargs)
            result: dict[str, Any] = response.data
            await asyncio.sleep(1.2)
            return result

    async def resolve_channels(self, names: list[str]) -> dict[str, str]:
        name_set = set(names)
        result: dict[str, str] = {}
        cursor: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "limit": 200,
                "types": "public_channel,private_channel",
            }
            if cursor:
                kwargs["cursor"] = cursor
            resp = await self._call("conversations_list", **kwargs)
            for channel in resp.get("channels", []):
                if channel["name"] in name_set:
                    result[channel["name"]] = channel["id"]
            next_cursor = resp.get("response_metadata", {}).get("next_cursor", "")
            if not next_cursor or len(result) == len(name_set):
                break
            cursor = next_cursor
        missing = name_set - set(result.keys())
        for name in missing:
            logger.warning("Channel '%s' not found in workspace, skipping", name)
        return result

    async def fetch_threads(self, channel_id: str, min_replies: int = 3) -> list[dict[str, Any]]:
        resp = await self._call("conversations_history", channel=channel_id, limit=100)
        messages: list[dict[str, Any]] = resp.get("messages", [])
        return [m for m in messages if m.get("reply_count", 0) >= min_replies]

    async def fetch_replies(self, channel_id: str, thread_ts: str) -> list[dict[str, Any]]:
        resp = await self._call(
            "conversations_replies", channel=channel_id, ts=thread_ts, limit=200
        )
        messages: list[dict[str, Any]] = resp.get("messages", [])
        return messages
