#!/usr/bin/env python3
"""The fixed evaluator for the heat formula (autoresearch's prepare.py): never edited by the loop.

Replays every work-hour checkpoint (6:00 through 17:00 PT, every work day) over the corpus
from bin/corpus.py, scores each thread with ``bin/formula.py``, ranks the board, and measures
it against Scott's own involvement: a thread he stepped into (replied to, did not start) is
one the board should have shown him BEFORE he got there; a thread he never stepped into is
one it should not have filled the top with.

- catch: for each thread he stepped into, the best it did on any board before his first
  reply, 1 inside the top 10 else 10/rank. "Would the board have shown it to me first."
- purity: per look, the share of the top 10 that he stepped into at some point. "Is the top
  of the board the kind of thread I actually join."

objective = (catch + purity) / 2, on the hourly looks so no formula can tune itself to three
instants of the day. The jev judge from bin/judge.py (p(needs) >= NEEDS_AT) is printed on the
``judge:`` lines as a second opinion and steers nothing.
Labels, checkpoints, the train/holdout split and the metric all live here, so the loop can
change the formula but never the test.

Prints greppable lines, one metric per line. The loop reads ``objective:`` (train) and reports
``holdout:`` next to it; a change is kept on train and judged, at the end, on holdout.

    uv run bin/replay.py                      # live formula, default schedule 9/13/16
    uv run bin/replay.py --schedule 9 16      # a quieter habit
    uv run bin/replay.py --json               # everything, for the explorer or a notebook
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import math
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

_BIN = Path(__file__).resolve().parent
_REPO_ROOT = _BIN.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from explore import _config_path, _default_corpus, _read_topics, build  # noqa: E402

from slack_dashboard.config import load_config  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_SCHEDULE = tuple(range(6, 18))
NEEDS_AT = 0.6  # judge probability at or above which a thread "needed Scott"
TOP = 10
HOLDOUT_DAYS = 7.0
RECALL_AT = (5, 10, 20)
# Live tiering (heat.classify_tier, relative): reported, not optimized.
TIER_HOT_COUNT = 3
TIER_WARM_COUNT = 10
TIER_FLOOR = 5.0
BLOOM_FINAL = 15
BLOOM_TAKEOFF = 8


def _load_formula(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("formula", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import formula from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["formula"] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def _features(
    t: dict[str, Any], c: dict[str, Any], k: dict[str, float], cw: float, formula: ModuleType
) -> tuple[Any, int, float] | None:
    """Features for thread ``t`` as seen at checkpoint ``c``; None when it is not on the board."""
    reps = [r for r in t["r"] if r[0] <= c["ts"]]
    last_ts = reps[-1][0] if reps else t["ts"]
    last_wh = reps[-1][1] if reps else t["wh"]
    if len(reps) < k["min_replies"]:
        return None
    if c["ts"] - last_ts > k["max_thread_age_days"] * 86400:
        return None
    people: dict[str, float] = {t["u"]: t["w"]}
    for r in reps:
        people[r[2]] = r[3]
    default_w = k["participant_weight"]
    people_points = sum(default_w if w == 0 else w for w in people.values())
    has_vip = any(w > default_w for w in people.values())
    window_s = k["velocity_window_minutes"] * 60
    in_window = sum(1 for r in reps if r[0] > c["ts"] - window_s)
    my_last = t["ts"] if t["me"] else 0.0
    for r in reps:
        if r[4]:
            my_last = r[0]
    after = sum(1 for r in reps if r[0] > my_last) if my_last else 0
    tp = t["tp"]
    f = formula.Features(
        channel_weight=cw,
        messages=1 + len(reps),
        people_points=people_points,
        people=len(people),
        has_vip=has_vip,
        hours_since_last=max(0.0, c["wh"] - last_wh),
        hours_alive=max(0.0, last_wh - t["wh"]),
        replies_in_window=in_window,
        replies_after_me=after,
        i_posted=bool(my_last),
        p_social=tp.get("social", 0.0),
        p_resolved=tp.get("resolved", 0.0),
        p_leader=tp.get("leader", 0.0),
        p_incident=tp.get("incident", 0.0),
        p_question=tp.get("question", 0.0),
        p_work=tp.get("work", 0.0),
    )
    return f, len(reps), last_ts


def _board(
    data: dict[str, Any], c: dict[str, Any], k: dict[str, float], formula: ModuleType
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for t in data["threads"]:
        if t["ts"] > c["ts"]:
            continue
        feats = _features(t, c, k, data["channel_weights"].get(t["ch"], 1.0), formula)
        if feats is None:
            continue
        f, _, _ = feats
        s = float(formula.score(f, k))
        if not math.isfinite(s):
            raise SystemExit(f"formula returned {s} for {t['id']} at {c['label']}")
        rows.append({"t": t, "score": s})
    rows.sort(key=lambda e: -e["score"])
    for i, e in enumerate(rows):
        e["rank"] = i + 1
        e["tier"] = (
            "hot"
            if i < TIER_HOT_COUNT and e["score"] >= TIER_FLOOR
            else "warm"
            if i < TIER_WARM_COUNT and e["score"] >= TIER_FLOOR
            else "cold"
        )
    return rows


def _takeoffs(data: dict[str, Any], scheduled: list[dict[str, Any]]) -> dict[str, float]:
    """Thread id -> checkpoint ts of the first look at which a late bloomer had taken off."""
    out: dict[str, float] = {}
    for t in data["threads"]:
        if 1 + len(t["r"]) < BLOOM_FINAL:
            continue
        for c in scheduled:
            n = 1 + sum(1 for r in t["r"] if r[0] <= c["ts"])
            if n >= BLOOM_TAKEOFF:
                mine = t["me"] or any(r[4] and r[0] <= c["ts"] for r in t["r"])
                if not mine:
                    out[t["id"]] = c["ts"]
                break
    return out


def _labeled_boards(
    data: dict[str, Any], scheduled: list[dict[str, Any]], k: dict[str, float], formula: ModuleType
) -> list[dict[str, Any]]:
    takeoffs = _takeoffs(data, scheduled)
    boards: list[dict[str, Any]] = []
    for i, c in enumerate(scheduled):
        next_ts = scheduled[i + 1]["ts"] if i + 1 < len(scheduled) else data["meta"]["pulled_at"]
        rows = _board(data, c, k, formula)
        by_id = {e["t"]["id"]: e for e in rows}
        replied: list[dict[str, Any]] = []
        too_new = unshowable = 0
        for t in data["threads"]:
            if t["me"] or not t["mf"]:
                continue
            if not (c["ts"] < t["mf"] <= next_ts):
                continue
            if t["ts"] > c["ts"]:
                too_new += 1
                continue
            e = by_id.get(t["id"])
            if e is None:
                unshowable += 1
                continue
            e["replied"] = True
            replied.append(e)
        bloom = [by_id[tid] for tid, ts in takeoffs.items() if ts == c["ts"] and tid in by_id]
        for e in bloom:
            e["bloom"] = True
        for e in rows:
            e["needed"] = bool(e.get("replied") or e.get("bloom"))
            e["judged"] = e["t"]["judge"].get("needs", 0.0) >= NEEDS_AT
            t = e["t"]
            e["joined"] = bool(t["mf"]) and not t["me"]
            e["before_me"] = e["joined"] and t["mf"] > c["ts"]
        boards.append(
            {
                "c": c,
                "rows": rows,
                "replied": replied,
                "bloom": bloom,
                "too_new": too_new,
                "unshowable": unshowable,
            }
        )
    return boards


def _metrics(boards: list[dict[str, Any]]) -> dict[str, Any]:
    m: dict[str, Any] = {
        "looks": len(boards),
        "positives": 0,
        "too_new": 0,
        "unshowable": 0,
        "bloomers": 0,
        "hot_rows": 0,
        "painted_rows": 0,
        "needed": 0,
        "needed_hot": 0,
        "needed_painted": 0,
    }
    r_hit = dict.fromkeys(RECALL_AT, 0)
    b_hit = dict.fromkeys(RECALL_AT, 0)
    ranks: list[int] = []
    for b in boards:
        m["too_new"] += b["too_new"]
        m["unshowable"] += b["unshowable"]
        for e in b["replied"]:
            m["positives"] += 1
            ranks.append(e["rank"])
            for n in RECALL_AT:
                if e["rank"] <= n:
                    r_hit[n] += 1
        for e in b["bloom"]:
            m["bloomers"] += 1
            for n in RECALL_AT:
                if e["rank"] <= n:
                    b_hit[n] += 1
        for e in b["rows"]:
            if e["tier"] == "hot":
                m["hot_rows"] += 1
            if e["tier"] != "cold":
                m["painted_rows"] += 1
            if e["needed"]:
                m["needed"] += 1
                m["needed_hot"] += e["tier"] == "hot"
                m["needed_painted"] += e["tier"] != "cold"
    for n in RECALL_AT:
        m[f"r{n}"] = r_hit[n] / m["positives"] if m["positives"] else 0.0
        m[f"b{n}"] = b_hit[n] / m["bloomers"] if m["bloomers"] else 0.0
    m["median_rank"] = float(sorted(ranks)[len(ranks) // 2]) if ranks else 0.0

    # The objective averages the two labels (replied-to, bloomer takeoff) with partial credit
    # below the fold: 1 inside the top 10, else 10/rank. Partial credit gives a change that
    # moves rank 30 to rank 12 a signal instead of nothing, which matters when the replied-to
    # label has 12 threads in train and recall@10 moves in 8-point steps.
    def credit(rows: list[dict[str, Any]]) -> float:
        return sum(min(1.0, 10 / e["rank"]) for e in rows) / len(rows) if rows else 0.0

    m["replied_credit"] = credit([e for b in boards for e in b["replied"]])
    m["bloom_credit"] = credit([e for b in boards for e in b["bloom"]])
    m["habit"] = (m["replied_credit"] + m["bloom_credit"]) / 2
    best: dict[str, float] = {}
    purity: list[float] = []
    jbest: dict[str, float] = {}
    jpurity: list[float] = []
    for b in boards:
        for e in b["rows"]:
            tid = e["t"]["id"]
            if e["before_me"]:
                best[tid] = max(best.get(tid, 0.0), min(1.0, TOP / e["rank"]))
            if e["judged"]:
                jbest[tid] = max(jbest.get(tid, 0.0), min(1.0, TOP / e["rank"]))
        head = b["rows"][:TOP]
        if head:
            purity.append(sum(1 for e in head if e["joined"]) / len(head))
            jpurity.append(sum(1 for e in head if e["judged"]) / len(head))
    m["joined"] = len(best)
    m["catch"] = sum(best.values()) / len(best) if best else 0.0
    m["purity"] = sum(purity) / len(purity) if purity else 0.0
    m["objective"] = (m["catch"] + m["purity"]) / 2
    m["judged"] = len(jbest)
    m["judge_catch"] = sum(jbest.values()) / len(jbest) if jbest else 0.0
    m["judge_purity"] = sum(jpurity) / len(jpurity) if jpurity else 0.0
    m["judge"] = (m["judge_catch"] + m["judge_purity"]) / 2
    return m


def run(
    formula: ModuleType,
    formula_path: Path,
    corpus: Path,
    schedule: tuple[int, ...],
    holdout_days: float,
    weights: Path | None = None,
) -> dict[str, Any]:
    config = load_config(_config_path())
    data = build(config, corpus)
    if weights is not None:
        data["channel_weights"] = {k: float(v) for k, v in json.loads(weights.read_text()).items()}
    judge = _read_topics(corpus.with_name("judge.jsonl"))
    if not judge:
        raise SystemExit("no judge.jsonl beside the corpus: run bin/judge.py first")
    for t in data["threads"]:
        t["judge"] = judge.get(t["id"], {})
    unjudged = sum(1 for t in data["threads"] if not t["judge"])
    if unjudged:
        logger.warning("run: %d threads have no judge record and count as not needed", unjudged)
    k = dict(formula.KNOBS)
    # Board membership is the test, not the model: a formula that could shrink the board
    # would move needed threads into "unshowable" and out of the denominator (seen run 25,
    # max age 1 day, objective +0.06 for free). These two come from the live config only.
    k["min_replies"] = float(config.fetch.min_replies)
    k["max_thread_age_days"] = float(config.heat.max_thread_age_days)
    scheduled = [c for c in data["checkpoints"] if c["h"] in schedule]
    split_ts = data["meta"]["pulled_at"] - holdout_days * 86400
    boards = _labeled_boards(data, scheduled, k, formula)
    train = [b for b in boards if b["c"]["ts"] < split_ts]
    holdout = [b for b in boards if b["c"]["ts"] >= split_ts]
    return {
        "boards": boards,
        "train": _metrics(train),
        "holdout": _metrics(holdout),
        "all": _metrics(boards),
        "schedule": list(schedule),
        "holdout_days": holdout_days,
        "formula_lines": formula_path.read_text().count("\n"),
    }


def _print(result: dict[str, Any]) -> None:
    tr, ho, al = result["train"], result["holdout"], result["all"]
    print(f"objective: {tr['objective']:.4f}")
    print(f"holdout: {ho['objective']:.4f}")
    print(f"all: {al['objective']:.4f}")
    for name, m in (("train", tr), ("holdout", ho)):
        print(
            f"{name}: looks={m['looks']} joined={m['joined']} "
            f"catch={m['catch']:.3f} purity={m['purity']:.3f}"
        )
        print(
            f"{name}_judge: {m['judge']:.3f} judged={m['judged']} "
            f"catch={m['judge_catch']:.3f} purity={m['judge_purity']:.3f}"
        )
        recall = " ".join(f"r{n}={m[f'r{n}']:.2f}" for n in RECALL_AT)
        bloom = " ".join(f"b{n}={m[f'b{n}']:.2f}" for n in RECALL_AT)
        print(
            f"{name}_habit: {m['habit']:.3f} positives={m['positives']} "
            f"too_new={m['too_new']} unshowable={m['unshowable']} bloomers={m['bloomers']} "
            f"{recall} {bloom} median_rank={m['median_rank']:.0f}"
        )
        needed = max(1, m["needed"])
        print(
            f"{name}_tiers: red_per_look={m['hot_rows'] / max(1, m['looks']):.1f} "
            f"painted_per_look={m['painted_rows'] / max(1, m['looks']):.1f} "
            f"needed_red={m['needed_hot'] / needed:.2f} "
            f"needed_painted={m['needed_painted'] / needed:.2f}"
        )
    print(f"formula_lines: {result['formula_lines']}")


def _print_misses(boards: list[dict[str, Any]]) -> None:
    """Every thread Scott joined that no board showed in the top 10 before he got there, at
    its best look, with the rows that outranked it (* = one he joined at some point). The
    loop's "think harder" step: see WHY the board put others first."""
    best: dict[str, dict[str, Any]] = {}
    for b in boards:
        for e in b["rows"]:
            if e["before_me"] and (
                e["t"]["id"] not in best or e["rank"] < best[e["t"]["id"]]["e"]["rank"]
            ):
                best[e["t"]["id"]] = {"b": b, "e": e}
    for item in sorted(best.values(), key=lambda x: x["b"]["c"]["ts"]):
        b, e = item["b"], item["e"]
        if e["rank"] <= TOP:
            continue
        t = e["t"]
        label = f"joined needs={t['judge'].get('needs', 0):.2f}"
        where = f"#{t['ch']} {t['title'][:60]}"
        print(f"{b['c']['label']} {label} rank={e['rank']} score={e['score']:.1f} {where}")
        print(f"  jev={ {q: round(v, 2) for q, v in t['tp'].items()} }")
        for top in b["rows"][:5]:
            tt = top["t"]
            flag = "*" if top["joined"] else " "
            print(
                f"  >{flag}{top['rank']:>2} {top['score']:6.1f} #{tt['ch']:<22} {tt['title'][:50]}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=Path, default=_default_corpus())
    parser.add_argument("--formula", type=Path, default=_BIN / "formula.py")
    parser.add_argument("--schedule", type=int, nargs="+", default=list(DEFAULT_SCHEDULE))
    parser.add_argument("--holdout-days", type=float, default=HOLDOUT_DAYS)
    parser.add_argument(
        "--weights", type=Path, default=None, help="JSON {channel: weight} replacing the yml prior"
    )
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    parser.add_argument("--misses", action="store_true", help="list needed rows outside the top 10")
    parser.add_argument("--log-level", default="ERROR")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")
    formula = _load_formula(args.formula)
    result = run(
        formula, args.formula, args.corpus, tuple(args.schedule), args.holdout_days, args.weights
    )
    if args.json:
        print(json.dumps(result, indent=1))
    elif args.misses:
        _print_misses(result["boards"])
    else:
        _print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
