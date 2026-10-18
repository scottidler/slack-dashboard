#!/usr/bin/env python3
"""Ask jev whether each corpus thread needed Scott: the label for the autoresearch loop.

A DEV tool (``uv run bin/judge.py``), not wired into the app or CI. Scott's own replies
measure his habit (he answers within minutes in channels he already watches), not what a
board should have surfaced. This asks the typed-decision model the question the board is
built to answer, with the reader's role spelled out in the state, and writes one record per
thread to ``judge.jsonl`` beside the corpus. ``bin/replay.py`` reads it as ground truth.

Two gut-checks per thread: ``needs`` (the reader should have read it) and ``urgent`` (the
same day). The reader description is the one piece of judgment in the file; change it and
re-rate when the role changes.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx

_BIN = Path(__file__).resolve().parent
for _path in (_BIN, _BIN.parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from topics import _default_corpus, _existing, rate_with_retry  # noqa: E402
from topics import _state as _topic_state  # noqa: E402

from slack_dashboard.topics import MODEL  # noqa: E402

logger = logging.getLogger(__name__)

READER = (
    "Director of Engineering for the Platform org: the SRE team (production reliability, "
    "Kubernetes, AWS, on-call, deploys, observability, cost), the Data Platform team "
    "(Airflow, Databricks, warehouses, pipelines, data access), and the Security team "
    "(acting head: vendor security reviews, access policy, incidents, compliance, AI policy). "
    "Also the escalation point for cross-team conflicts, hiring in those teams, budget, and "
    "anything a VP would ask them about."
)

QUESTIONS: dict[str, dict[str, Any]] = {
    "needs": {
        "type": "noul",
        "instructions": (
            "The reader should read this thread. It touches the reader's teams or "
            "responsibilities AND a director's awareness or action matters: a decision, an "
            "approval, an escalation, a risk, a cross-team disagreement, a policy question, "
            "a hiring or people matter, or something a VP would ask the reader about. Not a "
            "thread the reader's teams handle routinely among themselves, not a status note, "
            "not social chatter."
        ),
    },
    "urgent": {
        "type": "noul",
        "instructions": (
            "If the reader should read it, it needs them the same working day: an incident, "
            "a blocked person waiting on them, a deadline today, or a risk that grows with "
            "every hour. Not something that can wait until tomorrow."
        ),
    },
}


def _state(rec: dict[str, Any]) -> dict[str, Any]:
    """The topic state plus the reader description, so the question is asked for Scott."""
    return _topic_state(rec, {"reader": READER})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=Path, default=_default_corpus())
    parser.add_argument("--out", type=Path, default=None, help="default: judge.jsonl beside corpus")
    parser.add_argument("--limit", type=int, default=0, help="rate at most N threads (0 = all)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")

    api_key = os.getenv("TYPESAFE_API_KEY", "")
    if not api_key:
        print("TYPESAFE_API_KEY is unset", file=sys.stderr)
        return 2
    out = args.out or args.corpus.with_name("judge.jsonl")
    done = _existing(out)
    rated = 0
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
            probs = rate_with_retry(client, api_key, rec, QUESTIONS, _state(rec))
            dst.write(json.dumps({"id": tid, "model": MODEL, **probs}) + "\n")
            dst.flush()
            rated += 1
    print(f"{rated} threads judged -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
