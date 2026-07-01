"""The scoring harness: run a board through the SINGLE arithmetic path and grade it.

Two responsibilities, kept separate from the criteria (the judge) and the optimizer:

- :func:`rank_board` - the two-pass reference pattern: score every thread via
  ``heat_breakdown(...).overall`` at the fixed ``now``, sort descending, then
  ``classify_tier`` over the sorted list with each thread's post-sort rank. This NEVER
  re-implements the formula (that is the "Alternative 1" the strip doc rejected); it
  mirrors ``heat.rank_threads`` but pins ``now`` for determinism.
- :func:`score_board` - runs every criterion over a ranked board and returns
  ``(pass_count, failures, soft_distance)``. ``soft_distance`` is a continuous tie-breaker
  the optimizer minimises when the binary pass_count is flat, so the loop still has a
  gradient to follow.

The LIVE baseline config is built by :func:`live_config`: Scott's ``~/.config`` knob values
(velocity_weight 5.0, real channel/people weights, people_weight_cap 30), falling back to
code defaults for anything absent. That is the starting point the loop improves on - NOT
code defaults, which would tune from the wrong place.
"""

from __future__ import annotations

import logging

from slack_dashboard.config import HeatConfig
from slack_dashboard.heat import classify_tier, heat_breakdown
from slack_dashboard.thread import ThreadEntry
from tests.calibration import board, criteria
from tests.calibration.criteria import RankedThread

logger = logging.getLogger(__name__)


# Live channel-weights transcribed from ~/.config/slack-dashboard/slack-dashboard.yml.
_LIVE_CHANNEL_WEIGHTS: dict[str, float] = {
    "incidents": 3.0,
    "sre-sec": 2.8,
    "sre*": 2.5,
    "ask-security": 2.5,
    "data-platform*": 2.6,
    "eng-on-call": 2.2,
    "platform-internal": 2.0,
    "it-helpdesk": 1.6,
    "engineering-mgmt": 1.4,
    "engineering": 1.3,
    "tech-spec-reviews": 1.2,
    "scrum-of-scrums": 1.0,
    "ai-foundry": 0.9,
    "ai-technical": 0.9,
    "backstage": 0.6,
    "cloud-costs": 0.5,
    "staging-env": 0.5,
    "opex-monthly": 0.4,
}

# Live people-weights (VIP ids) transcribed from ~/.config. Only the ids the boards use
# need modelling, but the full live set is transcribed so the baseline is faithful.
_LIVE_PEOPLE_WEIGHTS: dict[str, float] = {
    "UPU1WE23F": 12,
    "UQ4PZELCE": 10,
    "U07R0G5UVJ8": 10,
    "U07MJKG9J9H": 10,
    "U023DJECU72": 8,
    "U0201PDEELC": 8,
    "U08KL0H3E6B": 8,
    "U09CFAHADG9": 8,
    "U02KD6JD12N": 8,
    "U03LCB49Y3C": 8,
    "U02B2DNC8M8": 8,
    "U09MEPL37JN": 8,
    "U01DSS69223": 8,
    "U0BAGH9MN05": 8,
    "U026Z2U5KK2": 8,
    "U02G958R3GA": 8,
    "U097N253M8E": 8,
    "U09H7T2PAPJ": 5,
    "U08RGQ8U52B": 5,
    "U08U2J0EVM2": 5,
}


def live_config() -> HeatConfig:
    """The LIVE baseline HeatConfig: Scott's ~/.config knobs + the PRE-REMODEL formula shape.

    The live channel/people/velocity weights and the people_weight_cap are transcribed
    verbatim from ``~/.config`` (velocity_weight 5.0, cap 30, the real weight maps). The
    re-model knobs are set to EMULATE the formula that was actually running when the
    pathology was observed - not the Phase 2 seed defaults, which already fix it:

    - ``base_cap``/``base_k`` set very large so ``base_norm = base_cap*v/(v+base_k)`` grows
      near-linearly in the everyday message range - i.e. the OLD unbounded base, so a big
      thread's volume dominates a small one (the "red-everywhere" defect #1).
    - ``atrophy_half_life_work_hours`` large so decay is slow, emulating the OLD 24h
      wall-clock linear ramp (``decay-half-life-hours: 24`` in the live file) - so an
      hours-idle thread stays near the top (the "atrophy too slow" defect #2).
    - ``tier_method`` absolute with the OLD ``hot-threshold: 50`` line, so a bounded
      threshold against an unbounded score paints nearly everything red.

    This is the baseline the loop must improve on. Baselining the Phase 2 seed defaults
    would tune from a board that is already fixed, hiding the very pathology the arena
    exists to break.
    """
    config = HeatConfig(
        velocity_weight=5.0,
        channel_weights=_LIVE_CHANNEL_WEIGHTS,
        people_weights=_LIVE_PEOPLE_WEIGHTS,
        people_weight_cap=30,
        # Pre-remodel formula-shape emulation (see docstring).
        base_cap=10000.0,
        base_k=10000.0,
        atrophy_half_life_work_hours=40.0,
        tier_method="absolute",
        tier_hot=50.0,
        tier_warm=20.0,
    )
    logger.debug(
        "live_config: velocity_weight=%.1f channels=%d people=%d people_weight_cap=%.0f "
        "base_cap=%.0f atrophy_half_life=%.0f tier_hot=%.0f",
        config.velocity_weight,
        len(config.channel_weights),
        len(config.people_weights),
        config.people_weight_cap,
        config.base_cap,
        config.atrophy_half_life_work_hours,
        config.tier_hot,
    )
    return config


def rank_board(
    threads: list[ThreadEntry],
    config: HeatConfig,
    now: float,
    self_user_id: str | None = board.SELF_ID,
) -> list[RankedThread]:
    """Two-pass rank: score every thread at ``now``, sort descending, then classify.

    Mirrors ``heat.rank_threads`` but pins ``now`` for determinism and returns
    (thread, rank, score, tier) tuples the criteria read. Never re-implements the formula.
    """
    logger.debug(
        "rank_board: threads=%d self_user_id=%s now=%.1f",
        len(threads),
        self_user_id,
        now,
    )
    scored = [(t, heat_breakdown(t, config, self_user_id, now).overall) for t in threads]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    total = len(scored)
    ranked = [
        RankedThread(
            thread=t,
            rank=rank,
            score=score,
            tier=classify_tier(score, rank, total, config),
        )
        for rank, (t, score) in enumerate(scored)
    ]
    hot = sum(1 for r in ranked if r.tier == "hot")
    logger.debug("rank_board: ranked=%d hot=%d", len(ranked), hot)
    return ranked


def score_board(
    config: HeatConfig,
    now: float = board.NOW,
) -> tuple[int, list[str], float]:
    """Run every criterion over both boards for ``config`` and grade the result.

    Returns ``(pass_count, failures, soft_distance)``:
    - ``pass_count`` - number of criteria that return True.
    - ``failures`` - the names of the criteria that return False.
    - ``soft_distance`` - a continuous badness scalar (sum of per-criterion soft
      penalties) the optimizer minimises to break ties when pass_count is flat.
    """
    busy = rank_board(board.busy_board(), config, now)
    contrast = rank_board(board.contrast_board(), config, now)

    pass_count = 0
    failures: list[str] = []
    soft_distance = 0.0
    for name, predicate in criteria.CRITERIA:
        ok = predicate(busy, contrast, config, now)
        if ok:
            pass_count += 1
        else:
            failures.append(name)
        soft_distance += criteria.soft_penalty(name, busy, contrast, config, now)

    logger.debug(
        "score_board: pass_count=%d/%d failures=%s soft_distance=%.3f",
        pass_count,
        len(criteria.CRITERIA),
        failures,
        soft_distance,
    )
    return pass_count, failures, soft_distance
