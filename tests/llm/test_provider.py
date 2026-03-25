from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic.types import TextBlock

from slack_dashboard.llm.provider import AnthropicProvider


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
    summary = await provider.generate_summary(
        [
            "We need to migrate the prod database",
            "I can handle the schema changes",
            "What about the rollback plan?",
            "Let's do it Saturday during maintenance window",
        ]
    )
    assert summary is not None
    assert "migrating" in summary.lower() or "database" in summary.lower()
    mock_client.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_generate_title_failure_returns_none() -> None:
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API error"))
    provider = AnthropicProvider(mock_client, model="claude-haiku-4-5-20251001")
    title = await provider.generate_title(["Some message"])
    assert title is None


@pytest.mark.asyncio
async def test_generate_summary_failure_returns_none() -> None:
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API error"))
    provider = AnthropicProvider(mock_client, model="claude-haiku-4-5-20251001")
    summary = await provider.generate_summary(["Some message"])
    assert summary is None
