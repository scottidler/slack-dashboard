import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from anthropic import AsyncAnthropic
from anthropic.types import Message, TextBlock

logger = logging.getLogger(__name__)

_TITLE_SEMAPHORE = asyncio.Semaphore(5)


@dataclass
class SummaryResult:
    """Return type for generate_summary.

    ``bullets`` is the markdown bulleted summary text, or None when the LLM
    call failed entirely.  ``tone`` is the linguistic tone score 0-3 ("0 cordial,
    1 tense, 2 pointed/frustrated, 3 openly hostile/escalating").

    In Phase 1, tone defaults to 0 - the TONE emit/parse/strip is Phase 2.
    Phase 2 will append a trailing ``TONE: <0-3>`` line to the LLM prompt,
    parse and clamp it here, and strip it from bullets before returning.
    """

    bullets: str | None
    tone: int = 0


def _extract_text(response: Message) -> str:
    for block in response.content:
        if isinstance(block, TextBlock):
            return block.text.strip()
    return ""


class LlmProvider(ABC):
    @abstractmethod
    async def generate_title(self, messages: list[str]) -> str | None: ...

    @abstractmethod
    async def generate_summary(self, messages: list[str]) -> SummaryResult: ...


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

    async def generate_summary(self, messages: list[str]) -> SummaryResult:
        """Generate a bulleted summary of the thread.

        Returns a SummaryResult with bullets=None on LLM failure (callers must
        check before storing).  tone defaults to 0 in Phase 1; Phase 2 will
        emit/parse/strip the TONE line from the prompt and response.
        """
        try:
            thread_content = "\n".join(messages)
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=500,
                system="You are a Slack thread summarizer. "
                "The user message contains raw Slack messages. "
                "Output ONLY a bulleted list of key points - no preamble, no lead sentence. "
                "Use markdown: start each bullet with '- '. "
                "Keep it concise - max 5 bullets. "
                "Focus on: decisions, action items, open questions, and anything urgent. "
                "Never ask for more information. Never refuse.",
                messages=[
                    {"role": "user", "content": f"Slack thread messages:\n\n{thread_content}"}
                ],
            )
            text = _extract_text(response) or None
            return SummaryResult(bullets=text, tone=0)
        except Exception:
            logger.exception("Failed to generate summary")
            return SummaryResult(bullets=None, tone=0)
