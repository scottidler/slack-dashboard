#!/usr/bin/env python3
"""The calibration loop (Karpathy Autoresearch): a DEV tool, NOT wired into CI.

Run it via ``uv run bin/calibrate.py``. It:

1. Baselines the LIVE knobs (Scott's ~/.config: velocity_weight 5.0, real channel/people
   weights, people_weight_cap 30) and reports the score. Expect many criteria to fail and
   the busy board to reproduce the pathology (nearly all red, the two idle threads pinned).
2. Perturbs ONE knob at a time from a bounded candidate grid, re-scores, keeps the change
   if it improves ``(pass_count, -soft_distance)`` lexicographically, discards otherwise.
   Caps at ``MAX_ITERS`` (<= 15) so it cannot over-optimize (the arena's iteration cap).
3. Writes the full baseline -> keep/discard -> final trace to a report artifact so the
   chosen knob values have visible provenance (the arena's "traceable actions" requirement).

The optimizer (this file) is kept strictly separate from the criteria (the judge in
``criteria.py``): it only reads ``score_board``'s ``(pass_count, failures, soft_distance)``
tuple and never inspects individual predicates, so it cannot bias itself toward a metric.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# Ensure the repo root is importable when run directly (uv run bin/calibrate.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from slack_dashboard.config import HeatConfig  # noqa: E402
from tests.calibration import board, criteria  # noqa: E402
from tests.calibration.score import live_config, rank_board, score_board  # noqa: E402

logger = logging.getLogger(__name__)

# The iteration cap (arena requirement: 5-15 or it over-optimizes).
MAX_ITERS = 15

# Default report artifact path (a knob-delta trace; committed as provenance).
DEFAULT_REPORT = _REPO_ROOT / "docs" / "design" / "2026-06-30-calibration-trace.md"


@dataclass(frozen=True)
class Knob:
    """One tunable knob and the bounded set of candidate values the loop may try.

    ``name`` is a HeatConfig attribute; ``candidates`` is a small, hand-bounded grid (the
    arena's "bounded environment" requirement). The loop tries each candidate that differs
    from the current value and keeps the first that improves the score.
    """

    name: str
    candidates: Sequence[float | str | int]


# The bounded candidate grid. Every knob the re-model exposes, with a handful of values
# spanning "off/gentle/aggressive". Ordering is deliberate: the FORMULA-SHAPE knobs
# (base_cap/base_k restore the hard ceiling, atrophy restores fast working-hours decay)
# come BEFORE the tier knobs, so the loop fixes the score's shape while it still moves the
# score - a greedy hill-climb that flipped tier_method first would mask an un-ceilinged base
# behind rank-based tiering and never restore the ceiling. Shape first, then coloring.
KNOBS: list[Knob] = [
    # Tiering method first: in relative mode the busy-board red count is governed by
    # tier_hot_count (rank-based), NOT by atrophy, so an over-aggressive atrophy stops
    # looking like a reds win and the loop won't over-commit it at the cost of weekend_frozen.
    Knob("tier_method", ["absolute", "relative"]),
    Knob("tier_hot_count", [3, 4, 5]),
    Knob("tier_warm_count", [8, 10, 12]),
    Knob("tier_floor", [3.0, 5.0, 8.0, 12.0]),
    # Formula shape: restore the hard base ceiling and fast working-hours atrophy.
    Knob("base_cap", [20.0, 30.0, 50.0, 80.0]),
    Knob("base_k", [10.0, 15.0, 25.0, 40.0]),
    Knob("atrophy_half_life_work_hours", [1.0, 1.5, 2.0, 3.0, 4.0]),
    Knob("activity_cap", [10.0, 20.0, 40.0, 60.0]),
    Knob("alive_weight", [0.0, 0.5, 1.0]),
    Knob("alive_k", [3.0, 6.0, 10.0]),
    Knob("involved_drop", [0.5, 0.7, 0.8, 0.9]),
    Knob("involved_rebuild_per_msg", [0.1, 0.15, 0.25]),
    # Absolute-mode thresholds (inert once tier_method is relative, but still swept).
    Knob("tier_hot", [10.0, 20.0, 30.0, 50.0]),
    Knob("tier_warm", [5.0, 10.0, 15.0, 20.0]),
]


def _fmt_config(config: HeatConfig) -> str:
    """The re-model knobs of a config, one per line, for the trace report."""
    fields = [
        "tier_method",
        "atrophy_half_life_work_hours",
        "base_cap",
        "base_k",
        "activity_cap",
        "alive_weight",
        "alive_k",
        "involved_drop",
        "involved_rebuild_per_msg",
        "tier_hot",
        "tier_warm",
        "tier_hot_count",
        "tier_warm_count",
        "tier_floor",
    ]
    return "\n".join(f"  {f} = {getattr(config, f)}" for f in fields)


def _score_tuple(config: HeatConfig) -> tuple[int, float]:
    """The (pass_count, -soft_distance) key the optimizer maximises lexicographically."""
    pass_count, _failures, soft = score_board(config)
    return pass_count, -soft


def _pathology_summary(config: HeatConfig) -> str:
    """Describe the busy board under ``config``: red count + the top 5 threads."""
    ranked = rank_board(board.busy_board(), config, board.NOW)
    hot = sum(1 for r in ranked if r.tier == "hot")
    lines = [f"hot(red) = {hot} of {len(ranked)}"]
    for r in ranked[:5]:
        lines.append(
            f"  #{r.rank + 1} [{r.tier:>4}] {r.score:7.2f}  {r.thread.channel_name}: "
            f"{r.thread.first_message.split(': ', 1)[-1]}"
        )
    top2 = ranked[:2]
    pinned = {"sandbox-google-workspace", "philo-migration"}
    top2_pinned = all(any(p in t.thread.first_message for p in pinned) for t in top2)
    lines.append(f"  two idle threads pinned top-2 = {top2_pinned}")
    return "\n".join(lines)


def calibrate() -> tuple[HeatConfig, list[str]]:
    """Run the keep/discard loop from the live baseline; return (final config, trace lines).

    Coordinate-descent hill-climb: SWEEP every knob (try each differing candidate, keep the
    best improving one, lexicographic on ``(pass_count, -soft_distance)``), then sweep again,
    until a full sweep changes nothing (converged) or ``MAX_ITERS`` knob changes are made.
    Re-sweeping lets the loop revisit an early knob once later knobs are set, so it does not
    lock a shape-phase choice (e.g. atrophy) that a tier choice would later want relaxed - a
    single-pass greedy climb would strand that local optimum. Each accepted knob change is
    one iteration against the cap.
    """
    trace: list[str] = []
    config = live_config()
    base_pass, base_failures, base_soft = score_board(config)
    trace.append("## Baseline (LIVE ~/.config knobs)")
    trace.append("")
    trace.append(f"pass_count = {base_pass}/{len(criteria.CRITERIA)}")
    trace.append(f"failures = {base_failures}")
    trace.append(f"soft_distance = {base_soft:.3f}")
    trace.append("")
    trace.append("busy-board pathology:")
    trace.append("```")
    trace.append(_pathology_summary(config))
    trace.append("```")
    trace.append("")
    logger.info("calibrate: baseline pass_count=%d failures=%s", base_pass, base_failures)

    trace.append("## Keep / discard trace")
    trace.append("")
    best = _score_tuple(config)
    iters = 0
    sweep = 0
    changed_in_sweep = True
    while changed_in_sweep and iters < MAX_ITERS:
        sweep += 1
        changed_in_sweep = False
        trace.append(f"### Sweep {sweep}")
        trace.append("")
        for knob in KNOBS:
            if iters >= MAX_ITERS:
                trace.append(f"(iteration cap {MAX_ITERS} reached; stopping)")
                break
            current = getattr(config, knob.name)
            best_candidate: float | str | int | None = None
            best_candidate_score = best
            for candidate in knob.candidates:
                if candidate == current:
                    continue
                trial = config.model_copy(update={knob.name: candidate})
                trial_score = _score_tuple(trial)
                verdict = "keep" if trial_score > best_candidate_score else "discard"
                trace.append(
                    f"- {knob.name}: {current} -> {candidate}  "
                    f"score={trial_score[0]}/{-trial_score[1]:.2f}  "
                    f"(best={best_candidate_score[0]}/{-best_candidate_score[1]:.2f}) -> {verdict}"
                )
                if trial_score > best_candidate_score:
                    best_candidate = candidate
                    best_candidate_score = trial_score
            if best_candidate is not None:
                config = config.model_copy(update={knob.name: best_candidate})
                best = best_candidate_score
                trace.append(f"  => CHANGED {knob.name}: {current} -> {best_candidate}")
                iters += 1
                changed_in_sweep = True
            else:
                trace.append(f"  => kept {knob.name} = {current} (no improving candidate)")
            trace.append("")

    final_pass, final_failures, final_soft = score_board(config)
    trace.append("## Final (winning knobs)")
    trace.append("")
    trace.append(f"pass_count = {final_pass}/{len(criteria.CRITERIA)}")
    trace.append(f"failures = {final_failures}")
    trace.append(f"soft_distance = {final_soft:.3f}")
    trace.append("")
    trace.append("winning re-model knobs:")
    trace.append("```")
    trace.append(_fmt_config(config))
    trace.append("```")
    trace.append("")
    trace.append("busy-board under winning knobs:")
    trace.append("```")
    trace.append(_pathology_summary(config))
    trace.append("```")
    logger.info(
        "calibrate: final pass_count=%d failures=%s iters=%d",
        final_pass,
        final_failures,
        iters,
    )
    return config, trace


def _report(trace: list[str]) -> str:
    header = [
        "# Calibration trace: heat re-model arena (Phase 4)",
        "",
        "Design doc: `docs/design/2026-06-30-heat-remodel-and-calibration-arena.md`",
        "",
        "Generated by `bin/calibrate.py` (a dev tool, not CI). Greedy single-knob hill-climb",
        "from the LIVE ~/.config baseline, capped at 15 knob changes. The optimizer reads only",
        "the (pass_count, soft_distance) score; the criteria are the judge in `criteria.py`.",
        "",
    ]
    return "\n".join(header + trace) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Heat-model calibration loop (dev tool).")
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="path to write the knob-delta trace report",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        help="logging level (debug, info, warning)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s %(message)s",
    )

    config, trace = calibrate()
    report = _report(trace)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report)

    # Echo the final knobs + score to stdout for a quick read without opening the file.
    print(report)
    print(f"Report written to {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
