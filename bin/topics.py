#!/usr/bin/env python3
"""Rate every corpus thread's topic with jev (TypeSafe's typed-decision model).

A DEV tool (``uv run bin/topics.py``), not wired into the app or CI. For each thread in the
corpus from ``bin/corpus.py`` it asks ``slack_dashboard.topics`` (the same questions, state
shape and parse the running app uses) and appends one record per thread to a sidecar jsonl
keyed on the thread id. ``bin/explore.py`` and ``bin/replay.py`` embed the probabilities so
the explorer and the autoresearch loop gate and vote on topic with the numbers the live
board sees.

Re-running is incremental: threads already rated are skipped. The model is pinned; a
response from anything that is not jev fails closed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# Ensure the repo root is importable when run directly (uv run bin/topics.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from slack_dashboard.topics import MODEL, QUESTIONS, rate_sync, state  # noqa: E402

logger = logging.getLogger(__name__)


def _default_corpus() -> Path:
    xdg = os.getenv("XDG_DATA_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "slack-dashboard" / "corpus.jsonl"


def _state(rec: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The corpus record's state, built by the app's own builder."""
    replies = rec["replies"]
    people = {rec["root"]["user"], *[m["user"] for m in replies]}
    return state(
        rec["channel"], rec["root"]["text"], [m["text"] for m in replies], len(people), extra
    )


def _existing(out: Path) -> set[str]:
    done: set[str] = set()
    if out.exists():
        with out.open() as fh:
            for line in fh:
                try:
                    done.add(json.loads(line)["id"])
                except (json.JSONDecodeError, KeyError):
                    continue
    return done


def rate(
    client: httpx.Client,
    api_key: str,
    rec: dict[str, Any],
    questions: dict[str, dict[str, Any]] = QUESTIONS,
    state_: dict[str, Any] | None = None,
) -> dict[str, float]:
    """One systemone call for one corpus record; returns {question: probability}.

    ``questions`` and ``state_`` default to the topic set so bin/judge.py can reuse the call
    with its own question and a state that names the reader.
    """
    probs = rate_sync(client, api_key, state_ or _state(rec), questions)
    logger.debug("rate: id=%s:%s probs=%s", rec["channel_id"], rec["thread_ts"], probs)
    return probs


def rate_with_retry(
    client: httpx.Client,
    api_key: str,
    rec: dict[str, Any],
    questions: dict[str, dict[str, Any]] = QUESTIONS,
    state_: dict[str, Any] | None = None,
) -> dict[str, float]:
    """``rate`` with one retry after a 429/529, the same policy as the app's TopicRater."""
    try:
        return rate(client, api_key, rec, questions, state_)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in (429, 529):
            raise
        logger.warning("rate limited (%s); sleeping 5s", exc.response.status_code)
        time.sleep(5)
        return rate(client, api_key, rec, questions, state_)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=Path, default=_default_corpus())
    parser.add_argument(
        "--out", type=Path, default=None, help="sidecar jsonl (default: topics.jsonl beside corpus)"
    )
    parser.add_argument("--limit", type=int, default=0, help="rate at most N threads (0 = all)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")

    api_key = os.getenv("TYPESAFE_API_KEY", "")
    if not api_key:
        print("TYPESAFE_API_KEY is unset", file=sys.stderr)
        return 2
    out = args.out or args.corpus.with_name("topics.jsonl")
    done = _existing(out)
    rated = 0
    tokens = 0
    with args.corpus.open() as src, out.open("a") as dst, httpx.Client() as client:
        for line in src:
            rec = json.loads(line)
            if rec.get("type") != "thread":
                continue
            tid = f"{rec['channel_id']}:{rec['thread_ts']}"
            if tid in done:
                continue
            if args.limit and rated >= args.limit:
                break
            probs = rate_with_retry(client, api_key, rec)
            dst.write(json.dumps({"id": tid, "model": MODEL, **probs}) + "\n")
            dst.flush()
            rated += 1
            tokens += len(json.dumps(_state(rec))) // 4
    print(f"{rated} threads rated -> {out} (~{tokens} input tokens)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
