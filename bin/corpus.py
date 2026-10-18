#!/usr/bin/env python3
"""Pull weeks of Slack thread history for the configured channels into one jsonl corpus.

A DEV tool (``uv run bin/corpus.py``), not wired into the app or CI. It reuses the app's
config (channel list, min-replies) and its ``SlackClient`` so the corpus reflects exactly
the threads the dashboard would have tracked, then writes one JSON record per thread with
the full reply timeline (ts, author, text, reactions). The replay harness and the knob
explorer (``bin/explore.py``) consume this file; nothing in the live app reads it.

Output layout (one JSON object per line):

- line 1: ``{"type": "meta", "self_user_id": ..., "pulled_at": ..., "weeks": ..., ...}``
- then:   ``{"type": "thread", "channel": ..., "channel_id": ..., "thread_ts": ...,
             "root": {"ts", "user", "text", "reactions"}, "replies": [{"ts", "user",
             "text", "reactions"}, ...]}``

Re-running is incremental: threads already in the output file are skipped (keyed on
channel_id + thread_ts), so a rate-limit abort can be resumed by running it again.
Reply pages wait 1.2s each (Slack tier 3), so a 4-week pull over 30 channels runs for
a while; run it in the background.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

# Ensure the repo root is importable when run directly (uv run bin/corpus.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from slack_dashboard.config import AppConfig, load_config  # noqa: E402
from slack_dashboard.slack.client import SlackClient, create_slack_client  # noqa: E402

logger = logging.getLogger(__name__)

# Slack tier-3 pacing for conversations.history pages (50+/min): 1.2s between pages.
_HISTORY_PAGE_SLEEP_S = 1.2


def _config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "slack-dashboard" / "slack-dashboard.yml"


def _default_out() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "slack-dashboard" / "corpus.jsonl"


def _message_record(m: dict[str, Any]) -> dict[str, Any]:
    """Project one Slack message onto the fields the corpus keeps."""
    reactions = [
        {"name": r.get("name", ""), "count": int(r.get("count", 0))} for r in m.get("reactions", [])
    ]
    return {
        "ts": m.get("ts", ""),
        "user": m.get("user") or m.get("bot_id") or "",
        "text": m.get("text", ""),
        "reactions": reactions,
    }


def _existing_keys(out: Path) -> set[tuple[str, str]]:
    """Keys of threads already in the output file (for incremental re-runs)."""
    keys: set[tuple[str, str]] = set()
    if not out.exists():
        return keys
    with out.open() as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "thread":
                keys.add((rec["channel_id"], rec["thread_ts"]))
    logger.debug("_existing_keys: %d threads already in %s", len(keys), out)
    return keys


async def _history_pages(
    slack: SlackClient, channel_id: str, oldest: float
) -> list[dict[str, Any]]:
    """Every parent message in ``channel_id`` newer than ``oldest``, following cursors.

    ``SlackClient.fetch_threads`` takes one 1000-message page and stops; a multi-week
    pull needs the cursor walk, so this pages the raw client under the same pacing.
    """
    client = slack._client  # noqa: SLF001 - dev tool reusing the app's authed client
    messages: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        kwargs: dict[str, Any] = {"channel": channel_id, "limit": 1000, "oldest": f"{oldest:.6f}"}
        if cursor:
            kwargs["cursor"] = cursor
        resp = await client.conversations_history(**kwargs)
        data: dict[str, Any] = resp.data  # type: ignore[assignment]
        page = data.get("messages", [])
        messages.extend(page)
        cursor = data.get("response_metadata", {}).get("next_cursor", "") or None
        logger.debug(
            "_history_pages: channel=%s page=%d total=%d more=%s",
            channel_id,
            len(page),
            len(messages),
            bool(cursor),
        )
        await asyncio.sleep(_HISTORY_PAGE_SLEEP_S)
        if not cursor:
            break
    return messages


async def pull(config: AppConfig, weeks: float, min_replies: int, out: Path, only: set[str]) -> int:
    """Fetch every thread with >= ``min_replies`` replies in the window; append to ``out``."""
    web = create_slack_client(config.slack.token)
    slack = SlackClient(web)
    self_user_id = await slack.resolve_self()
    now = time.time()
    oldest = now - weeks * 7 * 86400
    out.parent.mkdir(parents=True, exist_ok=True)
    existing = _existing_keys(out)
    written = 0
    with out.open("a") as fh:
        if not existing:
            meta = {
                "type": "meta",
                "self_user_id": self_user_id,
                "pulled_at": now,
                "oldest": oldest,
                "weeks": weeks,
                "min_replies": min_replies,
                "channels": sorted(config.channels),
            }
            fh.write(json.dumps(meta) + "\n")
        for name, channel_id in sorted(config.channels.items()):
            if only and name not in only:
                continue
            parents = await _history_pages(slack, channel_id, oldest)
            threads = [m for m in parents if m.get("reply_count", 0) >= min_replies]
            logger.info("pull: channel=%s parents=%d threads=%d", name, len(parents), len(threads))
            for parent in threads:
                thread_ts = parent["ts"]
                if (channel_id, thread_ts) in existing:
                    continue
                replies = await slack.fetch_replies(channel_id, thread_ts)
                # conversations.replies returns the root as its first message; drop it.
                tail = [m for m in replies if m.get("ts") != thread_ts]
                rec = {
                    "type": "thread",
                    "channel": name,
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                    "root": _message_record(parent),
                    "replies": [_message_record(m) for m in tail],
                }
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                existing.add((channel_id, thread_ts))
                written += 1
    logger.info("pull: wrote %d new threads to %s", written, out)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--weeks", type=float, default=4.0, help="history window (default 4)")
    parser.add_argument("--out", type=Path, default=_default_out(), help="jsonl output path")
    parser.add_argument(
        "--min-replies",
        type=int,
        default=None,
        help="threads with fewer replies are skipped (default: fetch.min-replies)",
    )
    parser.add_argument(
        "--channel", action="append", default=[], help="restrict to these channel names"
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")

    config = load_config(_config_path())
    if not config.slack.token or config.slack.token.startswith("${"):
        print("slack.token is unset: export SLACK_DASHBOARD_SLACK_USER_TOKEN", file=sys.stderr)
        return 2
    min_replies = args.min_replies if args.min_replies is not None else config.fetch.min_replies
    written = asyncio.run(pull(config, args.weeks, min_replies, args.out, set(args.channel)))
    print(f"{written} new threads -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
