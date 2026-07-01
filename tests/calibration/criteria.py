"""The binary desired-output spec: the judge, kept separate from the optimizer.

Each criterion is a true/false predicate over the two RANKED boards (the busy screenshot
board and the contrast weekend board), the config, and the fixed ``now``. They encode the
North Star ("bubble the threads that need my eyes RIGHT NOW; caught-up sinks") as
mechanically-checkable predicates:

- :func:`at_most_N_red` - count(hot) on the busy board <= N (a knob, start 3-5).
- :func:`lunchtime_threads_demoted` - the two ~1pm/2:30pm idle "pinned" threads are
  neither red nor in the top 5.
- :func:`active_recent_top3` - a thread with high recent activity (the in-score
  ``activity``/velocity signal, NOT alternation/structural_heat) is in the top 3.
- :func:`stale_is_cold` - a thread idle > 1 full working day is cold.
- :func:`weekend_frozen` - a Friday-4pm thread evaluated Monday-9am is still >= warm.
- :func:`involvement_drop_then_rebuild` - a thread the user just posted in drops below
  what it would rank at with no involvement drop, and a thread with unseen replies after
  his post ranks above one with none.
- :func:`vip_lift_capped` - VIP presence lifts a thread but the people term respects the cap.

Alongside each binary predicate is a soft penalty (:func:`soft_penalty`) giving the
optimizer a continuous gradient when the binary pass_count is flat. The soft signal never
overrides the binary judgement - it only breaks ties.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from slack_dashboard.config import HeatConfig
from slack_dashboard.heat import classify_tier, heat_breakdown
from slack_dashboard.thread import ThreadEntry
from slack_dashboard.worktime import business_hours_between
from tests.calibration import board

logger = logging.getLogger(__name__)

# The N in at_most_N_red. A knob; the doc says start 3-5. Seeded at 5, the loop may lower it.
N_RED = 5


@dataclass(frozen=True)
class RankedThread:
    """A thread paired with its post-sort rank, score, and tier for criteria to read.

    Defined here (not in score.py) so criteria have no import cycle: score.py imports this.
    """

    thread: ThreadEntry
    rank: int  # zero-based, descending by score
    score: float
    tier: str


# A criterion reads both ranked boards, the config, and the fixed now, returns pass/fail.
Predicate = Callable[[list[RankedThread], list[RankedThread], HeatConfig, float], bool]


def _find(ranked: list[RankedThread], channel: str, key_substr: str) -> RankedThread | None:
    """Locate a ranked thread by channel and a substring of its first_message key."""
    for r in ranked:
        if r.thread.channel_name == channel and key_substr in r.thread.first_message:
            return r
    return None


def at_most_N_red(
    busy: list[RankedThread],
    contrast: list[RankedThread],
    config: HeatConfig,
    now: float,
) -> bool:
    """count(tier == hot) on the busy board is at most N_RED."""
    hot = sum(1 for r in busy if r.tier == "hot")
    logger.debug("at_most_N_red: hot=%d N=%d", hot, N_RED)
    return hot <= N_RED


def lunchtime_threads_demoted(
    busy: list[RankedThread],
    contrast: list[RankedThread],
    config: HeatConfig,
    now: float,
) -> bool:
    """The two ~1pm/2:30pm idle "pinned" threads are neither red nor in the top 5."""
    sandbox = _find(busy, "sre-it", "sandbox-google-workspace")
    philo = _find(busy, "data-platform", "philo-migration")
    if sandbox is None or philo is None:
        return False
    ok = all(r.tier != "hot" and r.rank >= 5 for r in (sandbox, philo))
    logger.debug(
        "lunchtime_threads_demoted: sandbox(rank=%d tier=%s) philo(rank=%d tier=%s) -> %s",
        sandbox.rank,
        sandbox.tier,
        philo.rank,
        philo.tier,
        ok,
    )
    return ok


def active_recent_top3(
    busy: list[RankedThread],
    contrast: list[RankedThread],
    config: HeatConfig,
    now: float,
) -> bool:
    """A high recent-activity thread (in-score activity/velocity signal) is in the top 3.

    The incidents "prod-latency-spike" thread has a burst of replies in the last working
    hour, so its ``activity`` term is high. This uses the in-score signal, NOT
    structural_heat/alternation (which is glyph-only and not in the ranking score).
    """
    spike = _find(busy, "incidents", "prod-latency-spike")
    if spike is None:
        return False
    ok = spike.rank < 3
    logger.debug("active_recent_top3: spike(rank=%d) -> %s", spike.rank, ok)
    return ok


def stale_is_cold(
    busy: list[RankedThread],
    contrast: list[RankedThread],
    config: HeatConfig,
    now: float,
) -> bool:
    """A thread idle > 1 full working day (~12 work-hrs) is tier == cold.

    The sre "old-runbook" thread last posted Mon 10am; by Tue 8pm that is ~12 working
    hours idle, so it must be cold despite being a big VIP-laden thread.
    """
    stale = _find(busy, "sre", "sre-old-runbook")
    if stale is None:
        return False
    ok = stale.tier == "cold"
    logger.debug("stale_is_cold: stale(tier=%s score=%.2f) -> %s", stale.tier, stale.score, ok)
    return ok


def weekend_frozen(
    busy: list[RankedThread],
    contrast: list[RankedThread],
    config: HeatConfig,
    now: float,
) -> bool:
    """A Friday-4pm thread evaluated Monday-9am is still >= warm (weekend contributes 0).

    Re-derived explicitly (not read off a board's synthesized now): score the contrast
    board's Friday-4pm thread as if now were Monday 9am. Fri 4-6pm = 2 work-hrs, Mon
    6-9am = 3 work-hrs -> 5 working hours idle, well inside warm territory. The point is
    the weekend itself contributes ZERO, so it is not treated as ~65 wall-clock hours cold.
    """
    friday = None
    for t in board.contrast_board():
        if t.channel_name == "data-platform" and "friday-late-drop" in t.first_message:
            friday = t
            break
    if friday is None:
        return False
    # Monday 2026-06-29 09:00 PT as the evaluation instant.
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monday_9am = datetime(2026, 6, 29, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
    work_hours = business_hours_between(
        friday.last_activity.timestamp(), monday_9am, config.work_window
    )
    score = heat_breakdown(friday, config, board.SELF_ID, monday_9am).overall
    tier = classify_tier(score, 0, 1, config)
    ok = tier in ("hot", "warm")
    logger.debug(
        "weekend_frozen: work_hours=%.2f score=%.2f tier=%s -> %s",
        work_hours,
        score,
        tier,
        ok,
    )
    return ok


def involvement_drop_then_rebuild(
    busy: list[RankedThread],
    contrast: list[RankedThread],
    config: HeatConfig,
    now: float,
) -> bool:
    """Posting drops a thread; unseen replies after the user's post rebuild it.

    Two checks over the sre-it "claude-auth-rollout" thread (👤, one unseen reply after the
    user's post):
    1. Its score WITH the user as self drops below its score with no involvement (self=None).
    2. A synthetic copy with several unseen replies after the user's post scores higher than
       one with zero unseen replies (rebuild is monotone in unseen count).
    """
    involved = _find(busy, "sre-it", "claude-auth-rollout")
    if involved is None:
        return False
    thread = involved.thread
    with_self = heat_breakdown(thread, config, board.SELF_ID, now).overall
    without_involvement = heat_breakdown(thread, config, None, now).overall
    dropped = with_self < without_involvement

    zero_unseen = _rebuild_variant(thread, config, unseen=0)
    many_unseen = _rebuild_variant(thread, config, unseen=6)
    s_zero = heat_breakdown(zero_unseen, config, board.SELF_ID, now).overall
    s_many = heat_breakdown(many_unseen, config, board.SELF_ID, now).overall
    rebuilds = s_many > s_zero

    ok = dropped and rebuilds
    logger.debug(
        "involvement_drop_then_rebuild: with_self=%.2f without=%.2f dropped=%s "
        "s_zero=%.2f s_many=%.2f rebuilds=%s -> %s",
        with_self,
        without_involvement,
        dropped,
        s_zero,
        s_many,
        rebuilds,
        ok,
    )
    return ok


def _rebuild_variant(thread: ThreadEntry, config: HeatConfig, unseen: int) -> ThreadEntry:
    """A copy of ``thread`` whose user post is followed by exactly ``unseen`` replies.

    Preserves timing and channel; rewrites the reply authorship so the user posted, then
    ``unseen`` other-authored replies land after it. Used only to test rebuild monotonicity.
    """
    from slack_dashboard.thread import ReplyRecord

    replies = [
        ReplyRecord(ts=r.ts, author_id=r.author_id, text=r.text, is_root=r.is_root)
        for r in thread.replies
    ]
    if not replies:
        return thread
    # Place the user's last post before the final `unseen` replies.
    cut = len(replies) - unseen - 1
    cut = max(0, cut)
    replies[cut] = ReplyRecord(
        ts=replies[cut].ts, author_id=board.SELF_ID, text="me", is_root=replies[cut].is_root
    )
    for i in range(cut + 1, len(replies)):
        if replies[i].author_id == board.SELF_ID:
            replies[i] = ReplyRecord(
                ts=replies[i].ts,
                author_id="U_other",
                text=replies[i].text,
                is_root=replies[i].is_root,
            )
    participants = {a: 1 for a in {r.author_id for r in replies}}
    return ThreadEntry(
        channel_id=thread.channel_id,
        channel_name=thread.channel_name,
        thread_ts=thread.thread_ts,
        first_message=thread.first_message,
        started_by=thread.started_by,
        message_count=thread.message_count,
        participants=participants,
        last_activity=thread.last_activity,
        replies=replies,
        first_seen_ts=thread.first_seen_ts,
    )


def vip_lift_capped(
    busy: list[RankedThread],
    contrast: list[RankedThread],
    config: HeatConfig,
    now: float,
) -> bool:
    """VIP presence lifts a thread, but the people term never exceeds people_weight_cap.

    Two checks: (1) a thread with a VIP participant has a higher people_term than an
    otherwise-identical thread with only default-weight participants (lift exists); and
    (2) NO thread on the busy board has a people_term exceeding the cap (bounded).
    """
    if config.people_weight_cap <= 0:
        # No cap configured: only the lift half is meaningful.
        cap_ok = True
    else:
        cap_ok = all(
            heat_breakdown(r.thread, config, board.SELF_ID, now).people_term
            <= config.people_weight_cap + 1e-6
            for r in busy
        )
    vip = _find(busy, "data-platform", "philo-migration")  # VIP-laden
    plain = _find(busy, "engineering", "engineering-general")  # default-weight only
    if vip is None or plain is None:
        return cap_ok
    vip_term = heat_breakdown(vip.thread, config, board.SELF_ID, now).people_term
    plain_term = heat_breakdown(plain.thread, config, board.SELF_ID, now).people_term
    lift = vip_term > plain_term
    ok = lift and cap_ok
    logger.debug(
        "vip_lift_capped: vip_term=%.2f plain_term=%.2f cap=%.1f lift=%s cap_ok=%s -> %s",
        vip_term,
        plain_term,
        config.people_weight_cap,
        lift,
        cap_ok,
        ok,
    )
    return ok


# The ordered registry the harness iterates. Names are stable identifiers used in reports.
CRITERIA: list[tuple[str, Predicate]] = [
    ("at_most_N_red", at_most_N_red),
    ("lunchtime_threads_demoted", lunchtime_threads_demoted),
    ("active_recent_top3", active_recent_top3),
    ("stale_is_cold", stale_is_cold),
    ("weekend_frozen", weekend_frozen),
    ("involvement_drop_then_rebuild", involvement_drop_then_rebuild),
    ("vip_lift_capped", vip_lift_capped),
]


def soft_penalty(
    name: str,
    busy: list[RankedThread],
    contrast: list[RankedThread],
    config: HeatConfig,
    now: float,
) -> float:
    """A continuous badness scalar per criterion; the optimizer minimises the sum.

    Gives the loop a gradient when the binary pass_count is flat (e.g. reducing the number
    of reds even before crossing under N, or pushing a pinned thread further down the
    ranking). Never overrides the binary judgement - it only breaks ties. Returns 0.0 for
    criteria with no natural continuous relaxation.
    """
    if name == "at_most_N_red":
        hot = sum(1 for r in busy if r.tier == "hot")
        return float(max(0, hot - N_RED))
    if name == "lunchtime_threads_demoted":
        penalty = 0.0
        for channel, key in (
            ("sre-it", "sandbox-google-workspace"),
            ("data-platform", "philo-migration"),
        ):
            r = _find(busy, channel, key)
            if r is not None and r.rank < 5:
                penalty += float(5 - r.rank)
        return penalty
    if name == "active_recent_top3":
        r = _find(busy, "incidents", "prod-latency-spike")
        if r is not None and r.rank >= 3:
            return float(r.rank - 2)
        return 0.0
    if name == "stale_is_cold":
        r = _find(busy, "sre", "sre-old-runbook")
        if r is not None and r.tier != "cold":
            return 1.0
        return 0.0
    if name == "weekend_frozen":
        # Distance below warm for the Friday-4pm thread evaluated Monday-9am: gives the
        # loop a gradient AWAY from an over-aggressive atrophy that would freeze a
        # 5-work-hour-idle thread into cold, so it does not over-commit atrophy in the
        # shape phase. Penalty scales with how far the score sits below tier_warm.
        for t in board.contrast_board():
            if t.channel_name == "data-platform" and "friday-late-drop" in t.first_message:
                from datetime import datetime
                from zoneinfo import ZoneInfo

                monday = datetime(
                    2026, 6, 29, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles")
                ).timestamp()
                s = heat_breakdown(t, config, board.SELF_ID, monday).overall
                tier = classify_tier(s, 0, 1, config)
                if tier in ("hot", "warm"):
                    return 0.0
                # Continuous shortfall below the warm line (absolute mode) or the floor.
                target = config.tier_warm if config.tier_method == "absolute" else config.tier_floor
                return max(0.0, target - s) / max(target, 1.0)
        return 0.0
    return 0.0
