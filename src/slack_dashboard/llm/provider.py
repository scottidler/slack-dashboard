import asyncio
import logging
from abc import ABC, abstractmethod

from anthropic import AsyncAnthropic
from anthropic.types import Message, TextBlock

logger = logging.getLogger(__name__)

_TITLE_SEMAPHORE = asyncio.Semaphore(5)


def _extract_text(response: Message) -> str:
    for block in response.content:
        if isinstance(block, TextBlock):
            return block.text.strip()
    return ""


class LlmProvider(ABC):
    @abstractmethod
    async def generate_title(self, messages: list[str]) -> str | None: ...

    @abstractmethod
    async def generate_summary(self, messages: list[str]) -> str | None: ...


class AnthropicProvider(LlmProvider):
    def __init__(self, client: AsyncAnthropic, model: str) -> None:
        self._client = client
        self._model = model

    async def generate_title(self, messages: list[str]) -> str | None:
        async with _TITLE_SEMAPHORE:
            try:
                thread_content = "\n".join(messages)
                response = await self._client.messages.create(
                    model=self._model,
                    max_tokens=50,
                    system="You generate short titles for Slack threads. "
                    "The user message contains raw Slack messages. "
                    "Return a concise 5-8 word title. "
                    "No quotes, no punctuation, no explanation. Just the title.",
                    messages=[
                        {"role": "user", "content": f"Slack thread messages:\n\n{thread_content}"}
                    ],
                )
                return _extract_text(response) or None
            except Exception:
                logger.exception("Failed to generate title")
                return None

    async def generate_summary(self, messages: list[str]) -> str | None:
        try:
            thread_content = "\n".join(messages)
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=500,
                system="You are a Slack thread summarizer. "
                "The user message contains raw Slack messages. "
                "Output format: one sentence summary, then a bulleted list of key points. "
                "Use markdown: start each bullet with '- '. "
                "Keep it concise - max 5 bullets. "
                "Focus on: decisions, action items, open questions, and anything urgent. "
                "Never ask for more information. Never refuse.",
                messages=[
                    {"role": "user", "content": f"Slack thread messages:\n\n{thread_content}"}
                ],
            )
            return _extract_text(response) or None
        except Exception:
            logger.exception("Failed to generate summary")
            return None
