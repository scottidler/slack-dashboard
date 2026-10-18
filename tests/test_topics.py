from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from slack_dashboard.thread import ReplyRecord, ThreadEntry
from slack_dashboard.topics import (
    MAX_POST_CHARS,
    MAX_REPLIES,
    MODEL,
    QUESTIONS,
    TopicRater,
    clip,
    parse,
    request_body,
    state,
    thread_state,
)


def _answer(probs: dict[str, float], model: str = MODEL) -> dict[str, object]:
    return {
        "model": model,
        "answers": {name: {"noul": probs.get(name, 0.0)} for name in QUESTIONS},
        "usage": {"input_tokens": 1},
    }


def _entry() -> ThreadEntry:
    return ThreadEntry(
        channel_id="C123",
        channel_name="sre-internal",
        thread_ts="1.1",
        first_message="root <@U1> *bold*",
        started_by="U1",
        message_count=3,
        participants={"U1": 1, "U2": 2},
        last_activity=datetime(2026, 10, 1, 17, 0, tzinfo=UTC),
        replies=[
            ReplyRecord(ts=1.1, author_id="U1", text="root <@U1> *bold*", is_root=True),
            ReplyRecord(ts=1.2, author_id="U2", text="first reply", is_root=False),
            ReplyRecord(ts=1.3, author_id="U2", text="second reply", is_root=False),
        ],
    )


def test_clip_strips_mrkdwn_and_truncates() -> None:
    text = "*bold* and <https://x.y|a link>\nnext line " + "x" * (MAX_POST_CHARS * 2)
    out = clip(text)
    assert "*" not in out
    assert "<" not in out
    assert "\n" not in out
    assert len(out) == MAX_POST_CHARS


def test_state_shape_and_precomputed_counts() -> None:
    st = state("sre", "root?", ["a", "b"], people_count=3)
    assert st["channel"] == "#sre"
    assert st["message_count"] == 3
    assert st["people_count"] == 3
    assert st["posts"] == ["root: root?", "reply: a", "reply: b"]


def test_state_caps_replies() -> None:
    st = state("sre", "root", [f"r{i}" for i in range(MAX_REPLIES * 2)], people_count=1)
    assert len(st["posts"]) == MAX_REPLIES + 1


def test_state_extra_fields_ride_and_count_against_the_budget() -> None:
    reader = "R" * 3985
    st = state("sre", "root", ["a", "b"], people_count=1, extra={"reader": reader})
    assert st["reader"] == reader
    # The reader ate the reply budget: the root always rides, the replies did not fit.
    assert st["posts"] == ["root: root"]


def test_thread_state_reads_the_root_record_and_the_replies() -> None:
    st = thread_state(_entry())
    assert st["channel"] == "#sre-internal"
    assert st["message_count"] == 3
    assert st["people_count"] == 2
    assert st["posts"] == ["root: root @user bold", "reply: first reply", "reply: second reply"]


def test_request_body_pins_the_model() -> None:
    body = request_body({"channel": "#sre"}, QUESTIONS)
    assert body["model"] == MODEL
    assert set(body["questions"]) == set(QUESTIONS)


def test_parse_reads_every_question() -> None:
    probs = parse(_answer({"leader": 0.8, "resolved": 0.1}), QUESTIONS)
    assert probs["leader"] == 0.8
    assert probs["resolved"] == 0.1
    assert set(probs) == set(QUESTIONS)


def test_parse_fails_closed_on_a_non_jev_model() -> None:
    with pytest.raises(RuntimeError, match="non-jev"):
        parse(_answer({}, model="gpt-x"), QUESTIONS)


def _rater(handler: object) -> TopicRater:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return TopicRater(httpx.AsyncClient(transport=transport), api_key="k")


async def test_rater_returns_probabilities() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_answer({"leader": 0.7}))

    probs = await _rater(handler).rate(_entry())
    assert probs is not None
    assert probs["leader"] == 0.7
    assert seen[0].headers["Authorization"] == "Bearer k"


async def test_rater_returns_none_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    assert await _rater(handler).rate(_entry()) is None


async def test_rater_returns_none_on_non_jev_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_answer({}, model="other"))

    assert await _rater(handler).rate(_entry()) is None


async def test_rater_retries_once_after_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json=_answer({"leader": 0.2}))

    monkeypatch.setattr("slack_dashboard.topics.asyncio.sleep", AsyncMock())
    probs = await _rater(handler).rate(_entry())
    assert probs is not None
    assert probs["leader"] == 0.2
    assert len(calls) == 2
