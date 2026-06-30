from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic.types import TextBlock

from slack_dashboard.llm.provider import AnthropicProvider, SummaryResult, parse_tone


def _text_block(text: str) -> TextBlock:
    return TextBlock(type="text", text=text)


@pytest.mark.asyncio
async def test_generate_title() -> None:
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.content = [_text_block("Prod Database Migration Discussion")]
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    provider = AnthropicProvider(mock_client, model="claude-haiku-4-5-20251001")
    title = await provider.generate_title(
        [
            "We need to migrate the prod database",
            "I can handle the schema changes",
            "What about the rollback plan?",
        ]
    )
    assert title == "Prod Database Migration Discussion"
    mock_client.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_generate_summary() -> None:
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.content = [
        _text_block("The team discussed migrating the prod database. Key decisions: ...")
    ]
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    provider = AnthropicProvider(mock_client, model="claude-haiku-4-5-20251001")
    result = await provider.generate_summary(
        [
            "We need to migrate the prod database",
            "I can handle the schema changes",
            "What about the rollback plan?",
            "Let's do it Saturday during maintenance window",
        ]
    )
    assert isinstance(result, SummaryResult)
    assert result.bullets is not None
    assert "migrating" in result.bullets.lower() or "database" in result.bullets.lower()
    # No trailing TONE line in this response -> tone falls back to 0, bullets kept.
    assert result.tone == 0
    mock_client.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_generate_summary_parses_and_strips_tone() -> None:
    """A trailing TONE line is parsed into tone and stripped out of bullets."""
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.content = [
        _text_block("- They argued about the rollback\n- No agreement reached\nTONE: 3")
    ]
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    provider = AnthropicProvider(mock_client, model="claude-haiku-4-5-20251001")
    result = await provider.generate_summary(["You broke it", "No YOU broke it"])
    assert result.tone == 3
    assert result.bullets is not None
    assert "TONE" not in result.bullets
    assert result.bullets.endswith("No agreement reached")


@pytest.mark.asyncio
async def test_generate_title_failure_returns_none() -> None:
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API error"))
    provider = AnthropicProvider(mock_client, model="claude-haiku-4-5-20251001")
    title = await provider.generate_title(["Some message"])
    assert title is None


@pytest.mark.asyncio
async def test_generate_summary_failure_returns_summary_result_with_none_bullets() -> None:
    """On LLM failure, generate_summary returns SummaryResult(bullets=None) not None."""
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API error"))
    provider = AnthropicProvider(mock_client, model="claude-haiku-4-5-20251001")
    result = await provider.generate_summary(["Some message"])
    assert isinstance(result, SummaryResult)
    assert result.bullets is None
    assert result.tone == 0


# ---------------------------------------------------------------------------
# parse_tone: extract / coerce / clamp / strip
# ---------------------------------------------------------------------------


def test_parse_tone_extracts_and_strips() -> None:
    bullets, tone = parse_tone("- point one\n- point two\nTONE: 2")
    assert tone == 2
    assert bullets == "- point one\n- point two"
    assert "TONE" not in bullets


def test_parse_tone_case_insensitive() -> None:
    bullets, tone = parse_tone("- a\ntone: 1")
    assert tone == 1
    assert bullets == "- a"


def test_parse_tone_clamps_above_three() -> None:
    _, tone = parse_tone("- a\nTONE: 7")
    assert tone == 3


def test_parse_tone_clamps_below_zero() -> None:
    _, tone = parse_tone("- a\nTONE: -4")
    assert tone == 0


def test_parse_tone_missing_keeps_bullets_and_zero() -> None:
    text = "- a\n- b"
    bullets, tone = parse_tone(text)
    assert tone == 0
    assert bullets == text


def test_parse_tone_unparseable_keeps_bullets_and_zero() -> None:
    text = "- a\nTONE: high"
    bullets, tone = parse_tone(text)
    assert tone == 0
    assert bullets == text  # no digit matched -> line left intact, tone defaults to 0
