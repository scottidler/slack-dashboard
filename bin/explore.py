#!/usr/bin/env python3
"""Build the heat knob explorer: a self-contained HTML page over a corpus from bin/corpus.py.

A DEV tool (``uv run bin/explore.py``), not wired into the app or CI. It reads the corpus
jsonl plus the live config, precomputes the working-hours clock for every message with the
app's own ``business_hours_between`` (so the page never re-implements the work window),
resolves channel and people weights with the app's resolvers, and embeds the result into
``bin/explore.html``. The page replays the one-line heat formula at any checkpoint, lets
the shaping knobs be dragged, and scores recall against the threads Scott replied to.

The scoring formula IS re-implemented in the page's JavaScript (it has to run in the
browser against live knob values). That is deliberate and confined to this exploration
tool; the app keeps its single arithmetic path in ``heat.py``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Ensure the repo root is importable when run directly (uv run bin/explore.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from slack_dashboard.config import (  # noqa: E402
    AppConfig,
    load_config,
    resolve_channel_weight,
    resolve_person_weight,
)
from slack_dashboard.slack.mrkdwn import strip_mrkdwn  # noqa: E402
from slack_dashboard.worktime import business_hours_between  # noqa: E402

logger = logging.getLogger(__name__)

_TEMPLATE = Path(__file__).resolve().parent / "explore.html"
_FORMULA = Path(__file__).resolve().parent / "formula.py"
# The shared chart code the app page also loads. explore.html names it by its repo-relative
# path; the published page is one standalone file, so render_page inlines it in that tag's place.
_CHART_JS = _REPO_ROOT / "src" / "slack_dashboard" / "static" / "chart.js"
_CHART_TAG = '<script src="../src/slack_dashboard/static/chart.js"></script>'
# Shape knobs the page adds over heat.py. Live values = the live formula; the tuned column is
# what the autoresearch loop (bin/program.md) settled on and is hard-coded in formula.score.
# The explorer's names for the shape knobs the app now reads from the yml (HeatConfig
# atrophy_curve / atrophy_leader_stretch / leader_gate_floor); "live" is whatever the
# config says, built in build(). channel_exp is explorer-only (the prior's exponent).
_SHAPE_TUNED_ONLY = {"channel_exp": 1.0}
_SHAPE_TUNED = {
    "atrophy_curve": 1,
    "leader_stretch": 1.0,
    "leader_gate_floor": 0.3,
    "channel_exp": 1.0,
    # The tuned scores run lower than live (prior off, leader gate), so the live floor of 5
    # would leave most looks with 3-4 painted rows. Re-fitted below in the same scan.
    "tier_floor": 1.0,
    # Probability cuts for the fit on the involvement label (19% base rate, class-balanced):
    # 0.8 / 0.6 give 2.1 red and 10 painted per look, the same budget as fixed 3 / 10.
    "p_hot": 0.8,
    "p_warm": 0.6,
}
_TITLE_CHARS = 90
# The tuned channel prior, by org relationship (persona + channel membership, 2026-10-17):
# rooms made of Scott's direct reports first, the three internal Platform team rooms next,
# then rooms shared with his peer directors and his SVP, then ops and partner rooms in the
# live yml's order but ranked down. Weight, then the note the page shows on hover.
_CHANNEL_TIERS: dict[str, tuple[float, str]] = {
    "security-internal": (
        3.0,
        "direct reports: your security room; 6 members, 3/5 reports, 0/4 peers, 0/2 up",
    ),
    "platform-leadership": (
        3.0,
        "direct reports: you, your two managers; 4 members, 2/5 reports, 0/4 peers, 0/2 up",
    ),
    "sre-internal": (
        2.6,
        "Platform team room (SRE); 14 members, 1/5 reports, 0/4 peers, 0/2 up",
    ),
    "data-platform-internal": (
        2.6,
        "Platform team room (Data Platform); 10 members, 1/5 reports, 0/4 peers, 0/2 up",
    ),
    "platform-internal": (
        2.6,
        "Platform team room (all of Platform); 19 members, 2/5 reports, 0/4 peers, 0/2 up",
    ),
    "engineering-leadership": (
        2.4,
        "peers + SVP: you, two peer directors, your SVP; 4 members, 0/5 reports, 2/4 peers, 1/2 up",
    ),
    "tech-staff-team": (
        2.2,
        "peers + SVP: staff+ room; 12 members, 0/5 reports, 2/4 peers, 1/2 up",
    ),
    "engineering-mgmt": (
        2.2,
        "peers + SVP: every eng manager; 17 members, 2/5 reports, 3/4 peers, 1/2 up",
    ),
    "it-security": (
        2.0,
        "your security reports with IT; 10 members, 3/5 reports, 0/4 peers, 0/2 up",
    ),
    "opex-monthly": (
        1.6,
        "peers + SVP, but broad (58 people); 58 members, 2/5 reports, 4/4 peers, 1/2 up",
    ),
    "sre-sec": (
        1.5,
        "SRE x Security working room; 15 members, 4/5 reports, 1/4 peers, 0/2 up",
    ),
    "ask-security": (
        1.4,
        "company-wide asks to your security team; 202 members, 5/5 reports, 4/4 peers, 1/2 up",
    ),
    "sre": (1.3, "company-wide asks to SRE; 149 members, 5/5 reports, 3/4 peers, 1/2 up"),
    "data-platform": (
        1.3,
        "company-wide asks to Data Platform; 150 members, 5/5 reports, 3/4 peers, 1/2 up",
    ),
    "eng-on-call": (1.3, "on-call traffic; 106 members, 3/5 reports, 3/4 peers, 1/2 up"),
    "sre-it": (1.2, "SRE x IT; 11 members, 1/5 reports, 0/4 peers, 0/2 up"),
    "sre-solutions": (1.0, "SRE x Solutions; 15 members, 1/5 reports, 1/4 peers, 0/2 up"),
    "it-helpdesk": (
        1.0,
        "IT queue; mostly access asks; 370 members, 5/5 reports, 4/4 peers, 2/2 up",
    ),
    "tech-spec-reviews": (
        1.0,
        "peers review specs here; broad; 103 members, 4/5 reports, 4/4 peers, 1/2 up",
    ),
    "engineering": (0.8, "all of engineering; 192 members, 5/5 reports, 4/4 peers, 1/2 up"),
    "ai-foundry": (0.7, "AI interest group; 88 members, 5/5 reports, 3/4 peers, 1/2 up"),
    "ai-technical": (0.7, "AI interest group; 145 members, 5/5 reports, 4/4 peers, 2/2 up"),
    "enterprise-ai-onboarding-and-configuration": (
        0.6,
        "vendor rollout room; 50 members, 5/5 reports, 2/4 peers, 1/2 up",
    ),
    "backstage": (0.6, "other team's tool; 52 members, 4/5 reports, 2/4 peers, 0/2 up"),
    "proj-mintlify": (0.6, "project room; 9 members, 1/5 reports, 1/4 peers, 1/2 up"),
    "staging-env": (0.5, "env chatter; 62 members, 1/5 reports, 3/4 peers, 0/2 up"),
    "cloud-costs": (0.5, "cost reports"),
    "incidents": (2.0, "incident room (not in the corpus window)"),
}


def _config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "slack-dashboard" / "slack-dashboard.yml"


def _default_corpus() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "slack-dashboard" / "corpus.jsonl"


def _read_corpus(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    meta: dict[str, Any] = {}
    threads: list[dict[str, Any]] = []
    with path.open() as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("type") == "meta":
                meta = rec
            elif rec.get("type") == "thread":
                threads.append(rec)
    logger.debug("_read_corpus: %d threads, meta keys=%s", len(threads), sorted(meta))
    return meta, threads


def _read_topics(path: Path) -> dict[str, dict[str, float]]:
    """The jev topic sidecar from bin/topics.py: thread id -> {question: probability}."""
    topics: dict[str, dict[str, float]] = {}
    if not path.exists():
        logger.info("_read_topics: no sidecar at %s; topic knobs will be inert", path)
        return topics
    with path.open() as fh:
        for line in fh:
            rec = json.loads(line)
            tid = rec.pop("id")
            rec.pop("model", None)
            topics[tid] = {k: float(v) for k, v in rec.items()}
    logger.debug("_read_topics: %d threads rated", len(topics))
    return topics


def _tuned_knobs() -> dict[str, float]:
    """The numeric knobs from bin/formula.py, the file the autoresearch loop edits."""
    spec = importlib.util.spec_from_file_location("formula", _FORMULA)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import {_FORMULA}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["formula"] = module
    spec.loader.exec_module(module)
    knobs: dict[str, float] = dict(module.KNOBS)
    logger.debug("_tuned_knobs: %d knobs from %s", len(knobs), _FORMULA)
    return knobs


def _checkpoints(config: AppConfig, oldest: float, latest: float) -> list[dict[str, Any]]:
    """Every on-the-hour instant inside the work window between oldest and latest."""
    work = config.heat.work_window
    tz = work.tzinfo()
    workdays = work.work_weekdays()
    day = datetime.fromtimestamp(oldest, tz=tz).date()
    last = datetime.fromtimestamp(latest, tz=tz).date()
    out: list[dict[str, Any]] = []
    while day <= last:
        if day.weekday() in workdays:
            for hour in range(work.start_hour, work.end_hour):
                when = datetime(day.year, day.month, day.day, hour, tzinfo=tz)
                ts = when.timestamp()
                if oldest <= ts <= latest:
                    out.append(
                        {
                            "ts": ts,
                            "wh": business_hours_between(oldest, ts, work),
                            "h": hour,
                            "label": when.strftime("%a %Y-%m-%d %H:%M"),
                        }
                    )
        day += timedelta(days=1)
    logger.debug("_checkpoints: %d checkpoints", len(out))
    return out


def _title(text: str) -> str:
    one_line = " ".join(strip_mrkdwn(text).split())
    return one_line[:_TITLE_CHARS]


def _person_weight(user: str, config: AppConfig) -> float:
    """0 means 'the default participant weight' so the page can honor a dragged default."""
    weight = resolve_person_weight(user, config.heat)
    return 0.0 if weight == float(config.heat.participant_weight) else weight


def build(config: AppConfig, corpus: Path) -> dict[str, Any]:
    meta, threads = _read_corpus(corpus)
    topics = _read_topics(corpus.with_name("topics.jsonl"))
    judge = _read_topics(corpus.with_name("judge.jsonl"))
    for tid, probs in judge.items():
        topics.setdefault(tid, {}).update(probs)
    me = meta.get("self_user_id") or ""
    oldest = float(meta["oldest"])
    pulled_at = float(meta["pulled_at"])
    work = config.heat.work_window
    tz = work.tzinfo()
    my_replies = 0
    out_threads: list[dict[str, Any]] = []
    for rec in threads:
        root = rec["root"]
        root_ts = float(root["ts"])
        replies = sorted(rec["replies"], key=lambda m: float(m["ts"]))
        my_first = 0.0
        packed: list[list[Any]] = []
        for m in replies:
            ts = float(m["ts"])
            is_me = m["user"] == me
            if is_me:
                my_replies += 1
                if not my_first:
                    my_first = ts
            packed.append(
                [
                    ts,
                    business_hours_between(oldest, ts, work),
                    m["user"],
                    _person_weight(m["user"], config),
                    1 if is_me else 0,
                ]
            )
        tid = f"{rec['channel_id']}:{rec['thread_ts']}"
        out_threads.append(
            {
                "id": tid,
                "tp": topics.get(tid, {}),
                "ch": rec["channel"],
                "title": _title(root["text"]),
                "ts": root_ts,
                "wh": business_hours_between(oldest, root_ts, work),
                "u": root["user"],
                "w": _person_weight(root["user"], config),
                "me": 1 if root["user"] == me else 0,
                "mf": my_first,
                "r": packed,
            }
        )
    heat = config.heat
    cfg = {
        "reply_weight": heat.reply_weight,
        "participant_weight": heat.participant_weight,
        "people_weight_cap": heat.people_weight_cap,
        "base_cap": heat.base_cap,
        "base_k": heat.base_k,
        "atrophy_half_life_work_hours": heat.atrophy_half_life_work_hours,
        "alive_weight": heat.alive_weight,
        "alive_k": heat.alive_k,
        "max_thread_age_days": heat.max_thread_age_days,
        "velocity_weight": heat.velocity_weight,
        "velocity_window_minutes": heat.velocity_window_minutes,
        "activity_cap": heat.activity_cap,
        "followed_drop": heat.followed_drop,
        "followed_rebuild_per_msg": heat.followed_rebuild_per_msg,
        # Tiering. The live app knows "relative" (top-N counts) and "absolute" (score lines);
        # the explorer adds "median" (multiples of the board median), "gaps" (natural breaks
        # in the sorted scores) and "prob" (a logistic fit of score -> P(needs me) on the
        # corpus labels). Names here are the explorer's; "relative" maps to "fixed".
        "tier_method": "fixed" if heat.tier_method == "relative" else "absolute",
        "tier_hot_count": heat.tier_hot_count,
        "tier_warm_count": heat.tier_warm_count,
        "tier_floor": heat.tier_floor,
        "tier_hot": heat.tier_hot,
        "tier_warm": heat.tier_warm,
        "hot_mult": 4.0,
        "warm_mult": 1.5,
        "gap_top_n": 15,
        "gap_min_ratio": 1.3,
        "p_hot": 0.6,
        "p_warm": 0.3,
        "min_replies": config.fetch.min_replies,
        "board_rows": 25,
        "bloom_final": 15,
        "bloom_takeoff": 8,
        # Topic knobs (jev probabilities from bin/topics.py). The ones the app knows come
        # from the yml so "live" is the running board; the explorer-only ones start at 0.
        "social_drop": 0.0,
        "resolved_drop": heat.resolved_drop,
        "leader_weight": heat.leader_weight,
        "incident_weight": 0.0,
        "question_weight": 0.0,
        "atrophy_curve": 1 if heat.atrophy_curve == "hyperbolic" else 0,
        "leader_stretch": heat.atrophy_leader_stretch,
        "leader_gate_floor": heat.leader_gate_floor,
        **_SHAPE_TUNED_ONLY,
    }
    tuned = dict(cfg)
    tuned.update({k: v for k, v in _tuned_knobs().items() if k in cfg})
    tuned.update(_SHAPE_TUNED)
    channel_weights = {name: resolve_channel_weight(name, heat) for name in sorted(config.channels)}
    channel_weights_tuned = {
        name: _CHANNEL_TIERS.get(name, (channel_weights[name], ""))[0] for name in channel_weights
    }
    channel_notes = {
        name: _CHANNEL_TIERS[name][1] for name in channel_weights if name in _CHANNEL_TIERS
    }
    data = {
        "meta": {
            "self": me,
            "channels": meta.get("channels", sorted(config.channels)),
            "weeks": meta.get("weeks"),
            "oldest": oldest,
            "pulled_at": pulled_at,
            "pulled_label": datetime.fromtimestamp(pulled_at, tz=tz).strftime("%a %Y-%m-%d %H:%M"),
            "my_replies": my_replies,
            "rated": sum(1 for t in out_threads if "work" in t["tp"]),
            "judged": sum(1 for t in out_threads if "needs" in t["tp"]),
        },
        "work": {"start": work.start_hour, "end": work.end_hour},
        "cfg": cfg,
        "tuned": tuned,
        "channel_weights": channel_weights,
        "channel_weights_tuned": channel_weights_tuned,
        "channel_notes": channel_notes,
        "checkpoints": _checkpoints(config, oldest, pulled_at),
        "threads": out_threads,
    }
    logger.info(
        "build: threads=%d checkpoints=%d my_replies=%d",
        len(out_threads),
        len(data["checkpoints"]),
        my_replies,
    )
    return data


def render_page(payload: str) -> str:
    """The explorer as one standalone file: the data in, the shared chart script inlined."""
    template = _TEMPLATE.read_text()
    if _CHART_TAG not in template:
        raise ValueError(f"{_TEMPLATE} does not load {_CHART_TAG}")
    page = template.replace(_CHART_TAG, "<script>\n" + _CHART_JS.read_text() + "</script>")
    return page.replace("__DATA__", payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=Path, default=_default_corpus())
    parser.add_argument("--out", type=Path, required=True, help="HTML output path")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")
    config = load_config(_config_path())
    data = build(config, args.corpus)
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    html = render_page(payload)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html)
    print(f"{args.out} ({args.out.stat().st_size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
