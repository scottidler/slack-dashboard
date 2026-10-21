"""Topic probabilities from jev (TypeSafe's typed-decision model), the heat model's topic input.

One thread, one ``POST /v1/systemone`` call, a fixed set of yes/no (``noul``) questions, and
a ``{question: probability}`` answer. The questions, the state the model sees, and the
fail-closed parse live here so the app (``TopicRater``), the corpus tool (``bin/topics.py``)
and the judge (``bin/judge.py``) rate threads the same way: the autoresearch loop tuned the
heat knobs on ``bin/topics.py`` output, and the live board must feed the same numbers.

Questions are atomic gut-checks with described criteria, per the jev field guide: no dates,
no counting, no multi-hop references. Numbers the model would otherwise have to count
(message count, people count) are passed in as fields of the state. The model is pinned; an
answer from anything that is not jev fails closed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from slack_dashboard.slack.mrkdwn import strip_mrkdwn
from slack_dashboard.thread import ThreadEntry

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.14.0"
MAX_REPLIES = 12
MAX_POST_CHARS = 400
MAX_STATE_CHARS = 4000

# Each question is one gut-check with its criteria spelled out (bare labels rate worse).
QUESTIONS: dict[str, dict[str, Any]] = {
    "work": {
        "type": "noul",
        "instructions": (
            "The thread is about work: systems, services, projects, requests, incidents, "
            "vendors, process, planning, or decisions. Not social chatter."
        ),
    },
    "social": {
        "type": "noul",
        "instructions": (
            "The thread is social or personal: congratulations, celebration, cheering a team, "
            "farewells, jokes, food, pets, out-of-office notes, or thanks with no work content."
        ),
    },
    "leader": {
        "type": "noul",
        "instructions": (
            "A manager or director is being asked, or would be needed, to decide, approve, "
            "prioritize, assign an owner, or escalate. Individual contributors cannot close it "
            "on their own."
        ),
    },
    "incident": {
        "type": "noul",
        "instructions": (
            "An outage, production failure, degraded service, security issue, or data loss is "
            "being reported, investigated, or handled right now."
        ),
    },
    "access": {
        "type": "noul",
        "instructions": (
            "Someone is asking for access, permissions, a role, a license, or a tool to be "
            "enabled for them or their team."
        ),
    },
    "resolved": {
        "type": "noul",
        "instructions": (
            "By the last message the question is answered or the issue is fixed, and nobody is "
            "still waiting on a reply or an action."
        ),
    },
    "question": {
        "type": "noul",
        "instructions": (
            "The root post asks a question or makes a request that expects an answer from "
            "someone else, rather than announcing or sharing information."
        ),
    },
}

# Concurrent jev calls from the app. The poller fires one rating per thread change; this
# keeps a backfill storm from opening hundreds of sockets at once.
_RATE_SEMAPHORE = asyncio.Semaphore(4)


def clip(text: str) -> str:
    """One line of plain text, at most MAX_POST_CHARS, Slack wire tokens stripped."""
    one_line = " ".join(strip_mrkdwn(text).split())
    return one_line[:MAX_POST_CHARS]


def state(
    channel: str,
    root_text: str,
    reply_texts: list[str],
    people_count: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The state jev sees: channel, precomputed counts, the root, the first replies.

    ``extra`` lets the judge add fields (the reader description) without a second builder.
    The reply budget is MAX_STATE_CHARS minus the root and the extra fields, so a long
    thread is cut at the first reply that does not fit rather than truncated mid-post.
    """
    posts = [f"root: {clip(root_text)}"]
    budget = MAX_STATE_CHARS - len(posts[0])
    for value in (extra or {}).values():
        budget -= len(str(value))
    for text in reply_texts[:MAX_REPLIES]:
        line = f"reply: {clip(text)}"
        if budget - len(line) < 0:
            break
        posts.append(line)
        budget -= len(line)
    return {
        **(extra or {}),
        "channel": f"#{channel}",
        "message_count": 1 + len(reply_texts),
        "people_count": people_count,
        "posts": posts,
    }


def thread_state(entry: ThreadEntry) -> dict[str, Any]:
    """The state for a live ``ThreadEntry``: root from the retained replies, same clipping."""
    root = next((r.text for r in entry.replies if r.is_root), entry.first_message)
    replies = [r.text for r in entry.replies if not r.is_root]
    return state(entry.channel_name, root, replies, len(entry.participants))


def request_body(
    state_: dict[str, Any], questions: dict[str, dict[str, Any]], model: str = MODEL
) -> dict[str, Any]:
    return {"model": model, "state": state_, "questions": questions}


def parse(data: dict[str, Any], questions: dict[str, dict[str, Any]]) -> dict[str, float]:
    """``{question: probability}`` from a systemone answer; fails closed on a non-jev model."""
    model = str(data.get("model", ""))
    if not model.startswith("jev"):
        raise RuntimeError(f"non-jev answer: model={model!r}")
    answers = data["answers"]
    return {name: float(answers[name]["noul"]) for name in questions}


def rate_sync(
    client: httpx.Client,
    api_key: str,
    state_: dict[str, Any],
    questions: dict[str, dict[str, Any]] = QUESTIONS,
    model: str = MODEL,
    endpoint: str = ENDPOINT,
) -> dict[str, float]:
    """One blocking systemone call (the dev tools); raises on HTTP or model errors."""
    resp = client.post(
        endpoint,
        json=request_body(state_, questions, model),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    probs = parse(data, questions)
    logger.debug(
        "rate_sync: model=%s usage=%s probs=%s", data.get("model"), data.get("usage"), probs
    )
    return probs


class TopicRater:
    """Async jev client for the running app: one call per thread change, never raises.

    ``rate`` returns the probabilities, or None when the call fails for any reason (the
    thread then scores with neutral topic terms until the next change re-triggers it). A
    429/529 is retried once after a short sleep, matching ``bin/topics.py``.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        model: str = MODEL,
        endpoint: str = ENDPOINT,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._model = model
        self._endpoint = endpoint

    async def _post(self, state_: dict[str, Any]) -> dict[str, Any]:
        resp = await self._client.post(
            self._endpoint,
            json=request_body(state_, QUESTIONS, self._model),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def rate(self, entry: ThreadEntry) -> dict[str, float] | None:
        state_ = thread_state(entry)
        logger.debug(
            "TopicRater.rate: channel=%s thread_ts=%s messages=%d",
            entry.channel_name,
            entry.thread_ts,
            entry.message_count,
        )
        async with _RATE_SEMAPHORE:
            try:
                try:
                    data = await self._post(state_)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code not in (429, 529):
                        raise
                    logger.warning("TopicRater.rate: rate limited (%s); retrying in 5s", exc)
                    await asyncio.sleep(5)
                    data = await self._post(state_)
                probs = parse(data, QUESTIONS)
            except Exception:
                logger.exception(
                    "TopicRater.rate: failed for %s/%s", entry.channel_name, entry.thread_ts
                )
                return None
        logger.debug(
            "TopicRater.rate: channel=%s thread_ts=%s probs=%s",
            entry.channel_name,
            entry.thread_ts,
            probs,
        )
        return probs
