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
        self._history_semaphore = asyncio.Semaphore(1)
        self._replies_semaphore = asyncio.Semaphore(1)

    async def _call_history(self, method: str, **kwargs: Any) -> dict[str, Any]:
        async with self._history_semaphore:
            func = getattr(self._client, method)
            response = await func(**kwargs)
            result: dict[str, Any] = response.data
            await asyncio.sleep(1.2)
            return result

    async def _call_replies(self, method: str, **kwargs: Any) -> dict[str, Any]:
        async with self._replies_semaphore:
            func = getattr(self._client, method)
            response = await func(**kwargs)
            result: dict[str, Any] = response.data
            await asyncio.sleep(1.2)
            return result

    async def resolve_user(self, user_id: str) -> str:
        if not user_id:
            return ""
        try:
            resp = await self._call_history("users_info", user=user_id)
            user = resp.get("user", {})
            name: str = (
                user.get("profile", {}).get("display_name")
                or user.get("profile", {}).get("real_name")
                or user.get("name", user_id)
            )
            return name
        except Exception:
            return user_id

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
            resp = await self._call_history("conversations_list", **kwargs)
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

    async def fetch_threads(
        self,
        channel_id: str,
        min_replies: int = 3,
        oldest: str | None = None,
        max_pages: int = 5,
    ) -> list[dict[str, Any]]:
        all_threads: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max_pages):
            kwargs: dict[str, Any] = {"channel": channel_id, "limit": 200}
            if oldest:
                kwargs["oldest"] = oldest
            if cursor:
                kwargs["cursor"] = cursor
            resp = await self._call_history("conversations_history", **kwargs)
            messages: list[dict[str, Any]] = resp.get("messages", [])
            all_threads.extend(m for m in messages if m.get("reply_count", 0) >= min_replies)
            next_cursor = resp.get("response_metadata", {}).get("next_cursor", "")
            if not next_cursor:
                break
            cursor = next_cursor
        return all_threads

    async def fetch_replies(
        self,
        channel_id: str,
        thread_ts: str,
        oldest: str | None = None,
    ) -> list[dict[str, Any]]:
        all_messages: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            kwargs: dict[str, Any] = {"channel": channel_id, "ts": thread_ts, "limit": 1000}
            if oldest:
                kwargs["oldest"] = oldest
            if cursor:
                kwargs["cursor"] = cursor
            resp = await self._call_replies("conversations_replies", **kwargs)
            all_messages.extend(resp.get("messages", []))
            next_cursor = resp.get("response_metadata", {}).get("next_cursor", "")
            if not next_cursor:
                break
            cursor = next_cursor
        return all_messages
