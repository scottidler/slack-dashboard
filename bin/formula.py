"""The heat formula under autoresearch: the ONE file the loop edits.

``bin/replay.py`` (the fixed evaluator, never edited by the loop) computes the per-thread
features at a checkpoint and calls ``score`` here. Everything in this file is fair game:
the knob values, the shape of each term, new terms over the features already provided.
The features are the contract; the labels, the checkpoints and the metric live in replay.py.

``KNOBS`` starts as the live model (heat.py defaults plus the yml), so run 0 of any loop is
the live board. Read ``bin/program.md`` for the loop.
"""

from __future__ import annotations

from dataclasses import dataclass

# Live values. Keep the names: the explorer page and the yml use them.
KNOBS: dict[str, float] = {
    "reply_weight": 2.0,
    "participant_weight": 3.0,
    "people_weight_cap": 30.0,
    "base_cap": 50.0,
    "base_k": 15.0,
    "atrophy_half_life_work_hours": 0.5,
    "alive_weight": 0.0,
    "alive_k": 6.0,
    "velocity_weight": 5.0,
    "velocity_window_minutes": 30.0,
    "activity_cap": 20.0,
    "involved_drop": 0.8,
    "involved_rebuild_per_msg": 0.15,
    # Topic terms (jev probabilities). 0 = the live model, which has no topic terms.
    "social_drop": 0.0,
    "resolved_drop": 1.0,
    "leader_weight": 45,
    "incident_weight": 0.0,
    "question_weight": 0.0,
}


@dataclass(frozen=True)
class Features:
    """Everything replay.py knows about one thread at one checkpoint."""

    channel_weight: float  # the configured prior for the channel
    messages: int  # root + replies seen so far
    people_points: float  # sum of per-person weights (default or people-weights), uncapped
    people: int  # unique participants
    has_vip: bool  # someone with an above-default person weight is in the thread
    hours_since_last: float  # working hours of silence before this checkpoint
    hours_alive: float  # working hours from root to the last reply seen
    replies_in_window: int  # replies inside the velocity window before the checkpoint
    replies_after_me: int  # replies since my last post (0 if I never posted)
    i_posted: bool
    p_social: float  # jev probabilities, 0 when the thread was not rated
    p_resolved: float
    p_leader: float
    p_incident: float
    p_question: float
    p_work: float


def score(f: Features, k: dict[str, float]) -> float:
    """channel_weight x (base + activity + votes) x atrophy x alive_boost x damping x gates.

    This is heat.py's single arithmetic path with the explorer's topic terms added. Each
    term is named so an experiment can replace one shape without touching the rest.
    """
    people_points = f.people_points
    if k["people_weight_cap"] > 0:
        people_points = min(people_points, k["people_weight_cap"])
    volume = f.messages * k["reply_weight"] + people_points
    base = k["base_cap"] * volume / (volume + k["base_k"]) if volume > 0 else 0.0

    half = k["atrophy_half_life_work_hours"] * (0.5 + f.p_leader)
    atrophy = 1.0 / (1.0 + f.hours_since_last / half)

    window = k["velocity_window_minutes"]
    velocity = f.replies_in_window / window if window > 0 else 0.0
    activity = min(k["activity_cap"], velocity * k["velocity_weight"])

    alive_frac = f.hours_alive / (f.hours_alive + k["alive_k"]) if f.hours_alive > 0 else 0.0
    alive_boost = 1.0 + k["alive_weight"] * alive_frac * atrophy

    damping = 1.0
    if f.i_posted and k["involved_drop"] < 1:
        rebuild = min(1.0, f.replies_after_me * k["involved_rebuild_per_msg"])
        damping = min(1.0, k["involved_drop"] + (1 - k["involved_drop"]) * rebuild)

    votes = (
        k["leader_weight"] * f.p_leader
        + k["incident_weight"] * f.p_incident
        + k["question_weight"] * f.p_question
    )
    gates = (1 - k["social_drop"] * f.p_social) * (1 - k["resolved_drop"] * f.p_resolved)
    gates *= 0.3 + 0.7 * f.p_leader

    heat = f.channel_weight * (base + activity + votes) * atrophy * alive_boost * damping * gates
    return float(heat)
