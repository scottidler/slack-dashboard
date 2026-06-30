import asyncio
import logging
import re
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

    The prompt appends a trailing ``TONE: <0-3>`` line; ``parse_tone`` extracts
    and clamps it, and the trailing TONE line is stripped from ``bullets`` before
    it is stored/rendered.  On a missing/unparseable TONE the tone is 0 and the
    bullets are still kept (no thread is ever blocked on the tone signal).
    """

    bullets: str | None
    tone: int = 0


# Trailing "TONE: <n>" line the model is asked to emit.  Matched case-insensitively
# at the END of the response so a stray "TONE:" mid-summary cannot be mistaken for it.
_TONE_RE = re.compile(r"(?im)^\s*TONE:\s*(-?\d+).*?$\s*\Z")


def parse_tone(text: str) -> tuple[str, int]:
    """Split a summary response into (bullets, tone).

    Extracts the trailing ``TONE: <n>`` line, coerces ``<n>`` to an int, clamps
    it to 0-3, and strips that line out of the returned bullets.  When the line
    is missing or unparseable, returns ``(text, 0)`` so the bullets are kept and
    tone falls back to 0 (no thread is ever blocked on tone).
    """
    match = _TONE_RE.search(text)
    if match is None:
        logger.debug("parse_tone: no trailing TONE line found -> tone=0, bullets kept")
        return text, 0
    raw = int(match.group(1))
    tone = max(0, min(3, raw))
    bullets = text[: match.start()].rstrip()
    logger.debug("parse_tone: raw=%d clamped=%d bullets_len=%d", raw, tone, len(bullets))
    return bullets, tone


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
        """Generate a bulleted summary of the thread plus a 0-3 tone score.

        The prompt asks for the bullets followed by a single trailing
        ``TONE: <0-3>`` line; that line is parsed (coerced + clamped 0-3) and
        stripped out of the stored/rendered bullets.  A missing/unparseable TONE
        yields tone=0 with the bullets still kept.  Returns a SummaryResult with
        bullets=None only on outright LLM failure (callers must check before
        storing).
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
                "After the bullets, on the FINAL line, rate the linguistic tone of the "
                "exchange as 'TONE: <n>' where n is 0 cordial, 1 tense, "
                "2 pointed/frustrated, 3 openly hostile/escalating. "
                "Never ask for more information. Never refuse.",
                messages=[
                    {"role": "user", "content": f"Slack thread messages:\n\n{thread_content}"}
                ],
            )
            text = _extract_text(response)
            if not text:
                logger.debug("generate_summary: empty response -> bullets=None")
                return SummaryResult(bullets=None, tone=0)
            bullets, tone = parse_tone(text)
            logger.debug("generate_summary: bullets_len=%d tone=%d", len(bullets), tone)
            return SummaryResult(bullets=bullets, tone=tone)
        except Exception:
            logger.exception("Failed to generate summary")
            return SummaryResult(bullets=None, tone=0)
