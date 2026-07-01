"""Frozen regression test: the DEFAULT (shipping) HeatConfig must keep passing the
calibration arena's criteria on both boards, under ``otto ci``.

Phase 4 (see ``docs/design/2026-06-30-calibration-trace.md``) found that flipping
``tier_method`` to ``"relative"`` on top of the Phase 2 seed shapes is the one decisive
change that takes the busy board from 3/7 to 7/7 criteria passing. Phase 5 wrote that
winning knob into ``HeatConfig``'s defaults (``config.py``), so ``HeatConfig()`` with no
overrides now IS the winning config. This module freezes that result: if a future change
to the formula, a seed value, or ``classify_tier`` regresses the board back toward
all-red, this test goes red under ``otto ci`` - the arena cannot silently drift.

Assertions are on the SPECIFIC named criteria (not a brittle exact pass_count/soft_distance
equality), so a future change that trades one criterion's margin for another's is visible
by name rather than as an opaque count change.
"""

from __future__ import annotations

from slack_dashboard.config import HeatConfig
from tests.calibration import board, criteria, score


def _default_config() -> HeatConfig:
    """The shipping default HeatConfig - no overrides, i.e. exactly what ships."""
    return HeatConfig()


def test_default_config_uses_relative_tiering() -> None:
    # The one decisive knob from the calibration trace. If this default ever reverts to
    # "absolute" without a deliberate re-calibration, every criterion assertion below is
    # the regression signal, but this makes the specific knob failure legible on its own.
    config = _default_config()
    assert config.tier_method == "relative"


def test_default_config_passes_all_criteria_on_busy_board() -> None:
    config = _default_config()
    busy = score.rank_board(board.busy_board(), config, board.NOW)
    contrast = score.rank_board(board.contrast_board(), config, board.NOW)

    failures = [
        name
        for name, predicate in criteria.CRITERIA
        if not predicate(busy, contrast, config, board.NOW)
    ]
    assert failures == []


def test_default_config_at_most_n_red() -> None:
    config = _default_config()
    busy = score.rank_board(board.busy_board(), config, board.NOW)
    contrast = score.rank_board(board.contrast_board(), config, board.NOW)
    assert criteria.at_most_N_red(busy, contrast, config, board.NOW)


def test_default_config_demotes_lunchtime_threads() -> None:
    config = _default_config()
    busy = score.rank_board(board.busy_board(), config, board.NOW)
    contrast = score.rank_board(board.contrast_board(), config, board.NOW)
    assert criteria.lunchtime_threads_demoted(busy, contrast, config, board.NOW)


def test_default_config_surfaces_active_recent_thread() -> None:
    config = _default_config()
    busy = score.rank_board(board.busy_board(), config, board.NOW)
    contrast = score.rank_board(board.contrast_board(), config, board.NOW)
    assert criteria.active_recent_top3(busy, contrast, config, board.NOW)


def test_default_config_stale_thread_is_cold() -> None:
    config = _default_config()
    busy = score.rank_board(board.busy_board(), config, board.NOW)
    contrast = score.rank_board(board.contrast_board(), config, board.NOW)
    assert criteria.stale_is_cold(busy, contrast, config, board.NOW)


def test_default_config_weekend_is_frozen() -> None:
    config = _default_config()
    busy = score.rank_board(board.busy_board(), config, board.NOW)
    contrast = score.rank_board(board.contrast_board(), config, board.NOW)
    assert criteria.weekend_frozen(busy, contrast, config, board.NOW)


def test_default_config_involvement_drops_then_rebuilds() -> None:
    config = _default_config()
    busy = score.rank_board(board.busy_board(), config, board.NOW)
    contrast = score.rank_board(board.contrast_board(), config, board.NOW)
    assert criteria.involvement_drop_then_rebuild(busy, contrast, config, board.NOW)


def test_default_config_vip_lift_is_capped() -> None:
    config = _default_config()
    busy = score.rank_board(board.busy_board(), config, board.NOW)
    contrast = score.rank_board(board.contrast_board(), config, board.NOW)
    assert criteria.vip_lift_capped(busy, contrast, config, board.NOW)


def test_default_config_pass_count_is_full_on_both_fixtures() -> None:
    # score_board runs both fixtures (busy + contrast) internally; a full pass_count here
    # is the aggregate signal the per-criterion tests above break down by name.
    config = _default_config()
    pass_count, failures, _soft_distance = score.score_board(config, now=board.NOW)
    assert pass_count == len(criteria.CRITERIA)
    assert failures == []


# --- Contrast-board over-fit guard --------------------------------------------------------
#
# The CRITERIA registry above asserts most predicates only against the BUSY board (that is
# what those predicates read); only ``weekend_frozen`` exercises the contrast fixture. To
# make the contrast board a real over-fit guard (not just fixture ballast), assert the
# board-agnostic invariants directly on it here: a near-idle, mostly-cold board must NOT be
# painted all-red by relative tiering always filling its buckets, and its long-idle threads
# must go cold. Criteria that are BUSY-board-specific (lunchtime_threads_demoted,
# active_recent_top3 for the specific spike thread, involvement_drop_then_rebuild,
# vip_lift_capped) are deliberately NOT re-asserted on the contrast fixture: they name
# threads that only exist on the busy board, so they are not meaningful here.


def test_contrast_board_at_most_n_red() -> None:
    # at_most_N_red is board-agnostic (count hot on the given board). Re-check it on the
    # contrast board so a knob change cannot make relative tiering paint a quiet board red.
    config = _default_config()
    contrast = score.rank_board(board.contrast_board(), config, board.NOW)
    hot = sum(1 for r in contrast if r.tier == "hot")
    assert hot <= criteria.N_RED


def test_contrast_board_long_idle_threads_are_cold() -> None:
    # stale_is_cold's invariant ("a thread idle > 1 full working day is cold") applied to the
    # contrast fixture's long-idle threads: the Thursday it-helpdesk thread and the Sunday sre
    # FYI both last posted days before NOW (Tue 8pm), so both must be cold. This defends the
    # same "caught-up sinks" property stale_is_cold defends on the busy board.
    config = _default_config()
    contrast = score.rank_board(board.contrast_board(), config, board.NOW)
    stale_helpdesk = criteria._find(contrast, "it-helpdesk", "stale-helpdesk")
    weekend_fyi = criteria._find(contrast, "sre", "weekend-fyi")
    assert stale_helpdesk is not None and weekend_fyi is not None
    assert stale_helpdesk.tier == "cold"
    assert weekend_fyi.tier == "cold"
