"""Deterministic board fixtures for the calibration arena.

TWO fixtures (the panel's over-fit guard):

- :func:`busy_board` - the ~20 threads transcribed from Scott's 2026-06-30 ~8pm
  screenshot: real channels, message/participant counts, which participants are VIPs,
  the involvement (👤) flag on the four threads he posted in, heated/zombie flags where
  shown, and synthesized first/last-post timestamps against a FIXED ``now`` (2026-06-30
  20:00 PT). The two "pinned" threads - sre-it "Sandbox Google Workspace" and
  data-platform "Philo migration" - get last-posts of ~1pm / ~2:30pm, hours idle.
- :func:`contrast_board` - a near-idle weekend-morning / mostly-cold board, so the
  calibration loop cannot over-fit the single 20-thread snapshot.

Both boards evaluate against :data:`NOW` (float epoch). Timestamps are synthesized as
working-hours offsets from ``now`` so the busy board reproduces the observed pathology
(nearly everything red, the two idle threads pinned top-2) under the LIVE knobs, and the
contrast board is genuinely quiet.

``self_user_id`` is :data:`SELF_ID`: the four 👤 threads carry a reply authored by it.
VIP ids are transcribed from Scott's live ``~/.config`` people-weights (weight > the
default participant_weight of 3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from slack_dashboard.thread import ReplyRecord, ThreadEntry

# Fixed evaluation instant: 2026-06-30 20:00 PT (Tuesday). PT is UTC-7 (DST), so this is
# 2026-07-01 03:00 UTC. Two working hours past the 6pm close, so the 6pm-8pm tail counts
# for zero working hours (that is the point of the working-hours clock).
_PT = ZoneInfo("America/Los_Angeles")
NOW: float = datetime(2026, 6, 30, 20, 0, tzinfo=_PT).timestamp()

# The self user id (Scott). Threads flagged 👤 in the screenshot carry a reply by this id.
SELF_ID = "USELF"

# VIP ids transcribed from the live ~/.config people-weights (weight > participant_weight=3).
# A handful is enough to model "a VIP is present"; the exact weights live in the HeatConfig
# the harness baselines from, not here.
_VIP_BRYCE = "UPU1WE23F"  # Bryce York (weight 12)
_VIP_NICK = "UQ4PZELCE"  # Nick Below (weight 10)
_VIP_IAN = "U07R0G5UVJ8"  # Ian McEachern (weight 10)
_VIP_BEN = "U023DJECU72"  # Ben Horn (weight 8)


def _pt_ts(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    """A float epoch for a wall-clock instant in America/Los_Angeles."""
    return datetime(year, month, day, hour, minute, tzinfo=_PT).timestamp()


def _thread(
    key: str,
    channel: str,
    message_count: int,
    participant_ids: list[str],
    first_post: float,
    last_post: float,
    involved: bool = False,
    heated_tone: int = 0,
    resurrection_event_ts: float = 0.0,
    reply_gap_seconds: float = 0.0,
) -> ThreadEntry:
    """Build a ThreadEntry with a plausible reply timeline for the given counts.

    ``participant_ids`` are the distinct authors (VIP ids raise the people term). Replies
    are synthesized between ``first_post`` and ``last_post`` so ``time_alive``,
    ``velocity``, and involvement have real records to consume. When ``involved`` is set,
    SELF_ID authored the second-to-last reply (so there is exactly one unseen message
    after the user's last post, exercising the drop-and-rebuild floor). ``reply_gap_seconds``
    (when > 0) spaces the last two replies apart, so a fixture can be given a fresh burst
    in the velocity window regardless of when the thread started.
    """
    thread_ts = f"{first_post:.6f}"
    authors = list(participant_ids)
    if involved and SELF_ID not in authors:
        authors.append(SELF_ID)

    # Distribute message_count reply timestamps across [first_post, last_post], round-robin
    # over authors. The root is the first author; the tail lands on/near last_post.
    n = max(message_count, 1)
    span = max(last_post - first_post, 0.0)
    replies: list[ReplyRecord] = []
    for i in range(n):
        frac = i / (n - 1) if n > 1 else 1.0
        ts = first_post + span * frac
        author = authors[i % len(authors)]
        replies.append(ReplyRecord(ts=ts, author_id=author, text=f"msg {i}", is_root=(i == 0)))

    # Model involvement precisely: SELF_ID authored the second-to-last message so that
    # exactly one reply lands *after* the user's last post (rebuild input == 1).
    if involved and n >= 2:
        replies[-2] = ReplyRecord(ts=replies[-2].ts, author_id=SELF_ID, text="me", is_root=False)

    # Optionally pull the final reply(s) into a tight recent burst so velocity fires even
    # for a long-lived thread. Keeps the last reply at last_post.
    if reply_gap_seconds > 0 and n >= 2:
        replies[-1] = ReplyRecord(
            ts=last_post, author_id=replies[-1].author_id, text=replies[-1].text, is_root=False
        )
        replies[-2] = ReplyRecord(
            ts=last_post - reply_gap_seconds,
            author_id=replies[-2].author_id,
            text=replies[-2].text,
            is_root=False,
        )

    participants = {a: sum(1 for r in replies if r.author_id == a) for a in authors}

    return ThreadEntry(
        channel_id=f"C_{key}",
        channel_name=channel,
        thread_ts=thread_ts,
        first_message=f"{channel}: {key}",
        started_by=authors[0],
        message_count=message_count,
        participants=participants,
        last_activity=datetime.fromtimestamp(last_post, tz=UTC),
        replies=replies,
        first_seen_ts=first_post,
        resurrection_event_ts=resurrection_event_ts,
        heated_tone=heated_tone,
    )


def busy_board() -> list[ThreadEntry]:
    """The ~20-thread busy board transcribed from Scott's 2026-06-30 ~8pm screenshot.

    Timestamps are wall-clock PT on 2026-06-30 (the day of the screenshot) unless noted.
    The two pinned threads (sre-it Sandbox, data-platform Philo) last posted ~1pm/2:30pm -
    hours idle by the 8pm ``now`` - yet dominate under the live knobs; that is the
    pathology the loop must break. The four 👤 threads are the ones flagged involved in
    the screenshot: sre-it Sandbox, platform-internal, sre-it Claude-auth, it-helpdesk Codex.
    """
    d = (2026, 6, 30)  # the screenshot day
    threads: list[ThreadEntry] = [
        # --- The two "pinned" threads: big, VIP-laden, but idle since early afternoon. ---
        _thread(
            "sandbox-google-workspace",
            "sre-it",
            message_count=42,
            participant_ids=[_VIP_IAN, _VIP_BEN, "U_a", "U_b"],
            first_post=_pt_ts(*d, 9, 0),
            last_post=_pt_ts(*d, 13, 0),  # ~1pm, then idle
            involved=True,
        ),
        _thread(
            "philo-migration",
            "data-platform",
            message_count=38,
            participant_ids=[_VIP_BRYCE, _VIP_NICK, "U_c", "U_d"],
            first_post=_pt_ts(*d, 8, 30),
            last_post=_pt_ts(*d, 14, 30),  # ~2:30pm, then idle
            involved=False,
        ),
        # --- Genuinely fresh activity: small/medium threads active in the last work-hour. ---
        _thread(
            "prod-latency-spike",
            "incidents",
            message_count=14,
            participant_ids=[_VIP_NICK, "U_e", "U_f"],
            first_post=_pt_ts(*d, 17, 40),
            last_post=_pt_ts(*d, 17, 58),  # active right up to now-window
            heated_tone=2,
            reply_gap_seconds=90,
        ),
        _thread(
            "oncall-pager-noise",
            "eng-on-call",
            message_count=9,
            participant_ids=["U_g", "U_h"],
            first_post=_pt_ts(*d, 17, 30),
            last_post=_pt_ts(*d, 17, 55),
            reply_gap_seconds=120,
        ),
        _thread(
            "sec-review-turnaround",
            "ask-security",
            message_count=7,
            participant_ids=[_VIP_BEN, "U_i"],
            first_post=_pt_ts(*d, 17, 20),
            last_post=_pt_ts(*d, 17, 50),
            reply_gap_seconds=150,
        ),
        # --- Involved (👤) threads Scott posted in; one unseen reply after his post. ---
        _thread(
            "claude-auth-rollout",
            "sre-it",
            message_count=18,
            participant_ids=["U_j", "U_k"],
            first_post=_pt_ts(*d, 11, 0),
            last_post=_pt_ts(*d, 15, 30),
            involved=True,
        ),
        _thread(
            "platform-internal-sync",
            "platform-internal",
            message_count=12,
            participant_ids=[_VIP_IAN, "U_l"],
            first_post=_pt_ts(*d, 10, 15),
            last_post=_pt_ts(*d, 12, 45),
            involved=True,
        ),
        _thread(
            "codex-access-request",
            "it-helpdesk",
            message_count=6,
            participant_ids=["U_m"],
            first_post=_pt_ts(*d, 9, 45),
            last_post=_pt_ts(*d, 11, 30),
            involved=True,
        ),
        # --- Mid-afternoon threads: warm-ish, idle a few working hours. ---
        _thread(
            "data-platform-internal-etl",
            "data-platform-internal",
            message_count=21,
            participant_ids=[_VIP_BRYCE, "U_n", "U_o"],
            first_post=_pt_ts(*d, 12, 0),
            last_post=_pt_ts(*d, 15, 45),
        ),
        _thread(
            "sre-sec-audit",
            "sre-sec",
            message_count=16,
            participant_ids=[_VIP_BEN, "U_p"],
            first_post=_pt_ts(*d, 13, 0),
            last_post=_pt_ts(*d, 16, 15),
        ),
        _thread(
            "eng-mgmt-planning",
            "engineering-mgmt",
            message_count=11,
            participant_ids=["U_q", "U_r"],
            first_post=_pt_ts(*d, 14, 0),
            last_post=_pt_ts(*d, 16, 30),
        ),
        _thread(
            "tech-spec-review-x",
            "tech-spec-reviews",
            message_count=8,
            participant_ids=["U_s", "U_t"],
            first_post=_pt_ts(*d, 13, 30),
            last_post=_pt_ts(*d, 16, 0),
        ),
        # --- Stale threads: idle > 1 full working day (must go cold). ---
        _thread(
            "sre-old-runbook",
            "sre",
            message_count=25,
            participant_ids=[_VIP_NICK, "U_u", "U_v"],
            first_post=_pt_ts(2026, 6, 29, 9, 0),  # Monday
            last_post=_pt_ts(2026, 6, 29, 10, 0),  # Mon 10am -> ~12 work-hrs idle by Tue 8pm
        ),
        _thread(
            "cloud-costs-review",
            "cloud-costs",
            message_count=30,
            participant_ids=["U_w", "U_x"],
            first_post=_pt_ts(2026, 6, 29, 8, 0),
            last_post=_pt_ts(2026, 6, 29, 9, 0),
        ),
        _thread(
            "opex-monthly-close",
            "opex-monthly",
            message_count=19,
            participant_ids=["U_y"],
            first_post=_pt_ts(2026, 6, 26, 15, 0),  # last Friday
            last_post=_pt_ts(2026, 6, 26, 16, 0),
        ),
        # --- Low-signal / noise channels, kept visible but low. ---
        _thread(
            "staging-env-flap",
            "staging-env",
            message_count=5,
            participant_ids=["U_z"],
            first_post=_pt_ts(*d, 14, 0),
            last_post=_pt_ts(*d, 15, 0),
        ),
        _thread(
            "backstage-plugin",
            "backstage",
            message_count=4,
            participant_ids=["U_aa", "U_ab"],
            first_post=_pt_ts(*d, 12, 30),
            last_post=_pt_ts(*d, 14, 15),
        ),
        _thread(
            "ai-foundry-experiment",
            "ai-foundry",
            message_count=13,
            participant_ids=["U_ac", "U_ad"],
            first_post=_pt_ts(*d, 11, 45),
            last_post=_pt_ts(*d, 15, 15),
        ),
        _thread(
            "ai-technical-q",
            "ai-technical",
            message_count=6,
            participant_ids=["U_ae"],
            first_post=_pt_ts(*d, 13, 45),
            last_post=_pt_ts(*d, 16, 45),
        ),
        _thread(
            "engineering-general",
            "engineering",
            message_count=10,
            participant_ids=["U_af", "U_ag"],
            first_post=_pt_ts(*d, 15, 0),
            last_post=_pt_ts(*d, 17, 0),
        ),
    ]
    return threads


def contrast_board() -> list[ThreadEntry]:
    """A near-idle weekend-morning board evaluated at the same ``NOW``.

    Everything last posted over the prior weekend or is a tiny quiet monologue, so almost
    nothing should be hot. Its purpose is to keep the loop honest: knobs tuned to break the
    busy board's all-red pathology must ALSO leave this quiet board mostly cold, not paint
    top-N red just because relative tiering always fills its buckets.

    One thread models the weekend-frozen criterion: a Friday-4pm thread that, evaluated
    Monday, is only ~5 working hours idle (Fri 4-6pm = 2, Mon 6-9am = 3) - it must stay
    at least warm. Here it is evaluated against NOW (Tue 8pm) via its own working-hours
    math, which the weekend_frozen criterion re-derives explicitly.
    """
    threads: list[ThreadEntry] = [
        _thread(
            "sat-monologue",
            "engineering",
            message_count=1,
            participant_ids=["U_h1"],
            first_post=_pt_ts(2026, 6, 27, 10, 0),  # Saturday
            last_post=_pt_ts(2026, 6, 27, 10, 0),
        ),
        _thread(
            "weekend-fyi",
            "sre",
            message_count=3,
            participant_ids=["U_h2", "U_h3"],
            first_post=_pt_ts(2026, 6, 28, 11, 0),  # Sunday
            last_post=_pt_ts(2026, 6, 28, 12, 0),
        ),
        _thread(
            "friday-late-drop",
            "data-platform",
            message_count=8,
            participant_ids=[_VIP_BRYCE, "U_h4"],
            first_post=_pt_ts(2026, 6, 26, 14, 0),  # Friday afternoon
            last_post=_pt_ts(2026, 6, 26, 16, 0),  # Fri 4pm
        ),
        _thread(
            "stale-helpdesk",
            "it-helpdesk",
            message_count=5,
            participant_ids=["U_h5"],
            first_post=_pt_ts(2026, 6, 25, 9, 0),  # Thursday
            last_post=_pt_ts(2026, 6, 25, 10, 0),
        ),
        _thread(
            "quiet-costs",
            "cloud-costs",
            message_count=2,
            participant_ids=["U_h6"],
            first_post=_pt_ts(2026, 6, 26, 9, 0),
            last_post=_pt_ts(2026, 6, 26, 9, 30),
        ),
        _thread(
            "one-fresh-monday-thing",
            "incidents",
            message_count=6,
            participant_ids=["U_h7", "U_h8"],
            first_post=_pt_ts(*(2026, 6, 30), 17, 30),
            last_post=_pt_ts(*(2026, 6, 30), 17, 55),
            reply_gap_seconds=120,
        ),
    ]
    return threads
