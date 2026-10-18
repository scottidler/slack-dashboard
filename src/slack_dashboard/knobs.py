"""The heat knobs a viewer may override per request, and the request-scoped config they build.

``KNOBS`` is the one list of what the site's knob panel shows. A request carries overrides
as ``heat.<kebab-key>=<value>`` query parameters (plus ``heat.channel-weights.<channel>`` and
``heat.people-weights.<user-id>`` for the two maps). ``parse_overrides`` reads them,
``apply_overrides`` merges them over the yml's ``heat`` block and re-validates through
``HeatConfig``. Nothing here writes to the yml, the poller, or the shared thread entries: the
result is a config object that lives for one request.
"""

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from pydantic import ValidationError

from slack_dashboard.config import AppConfig, HeatConfig
from slack_dashboard.heat import Ranked, heat_breakdown

logger = logging.getLogger(__name__)

PREFIX = "heat."
# The two weight maps, overridden one name at a time: heat.channel-weights.<channel>=<float>.
MAPS = ("channel-weights", "people-weights")


@dataclass(frozen=True)
class Knob:
    key: str  # HeatConfig field name (snake); kebab in the query and the panel
    group: str  # size | time | burst | following | topics | tiers
    label: str
    lo: float
    hi: float
    step: float
    help: str
    choices: tuple[str, ...] = ()  # atrophy_curve, tier_method
    narrows_only: bool = False  # max_thread_age_days

    @property
    def query_key(self) -> str:
        """The query parameter that overrides this knob, e.g. ``heat.velocity-weight``."""
        return PREFIX + self.key.replace("_", "-")


# Ranges copy bin/explore.html's, corrected where HeatConfig disagrees: the two int weights
# step by 1, the leader stretch stops below 2 (HeatConfig requires strictly < 2), and
# activity-cap starts at 1 (0 is rejected: it would zero the burst term).
KNOBS: tuple[Knob, ...] = (
    Knob(
        "reply_weight",
        "size",
        "reply weight",
        0,
        5,
        1,
        "Volume points per message. Volume feeds base through the saturation curve.",
    ),
    Knob(
        "participant_weight",
        "size",
        "person weight",
        0,
        12,
        1,
        "Volume points per unique person without their own people-weight.",
    ),
    Knob(
        "people_weight_cap",
        "size",
        "people cap (0=off)",
        0,
        80,
        5,
        "Ceiling on the total people contribution, so a pile of VIPs cannot run away.",
    ),
    Knob(
        "base_cap",
        "size",
        "base cap",
        10,
        200,
        5,
        "The hard ceiling base approaches as volume grows; sets the scale of the score.",
    ),
    Knob(
        "base_k",
        "size",
        "base k (half point)",
        1,
        60,
        1,
        "Volume at which base is half the cap.",
    ),
    Knob(
        "atrophy_half_life_work_hours",
        "time",
        "half-life (work h)",
        0.25,
        12,
        0.25,
        "Working hours of silence that halve the score.",
    ),
    Knob(
        "atrophy_curve",
        "time",
        "decay curve",
        0,
        0,
        0,
        "exponential: 0.5 ^ (t / h). hyperbolic: 1 / (1 + t / h), same half point, longer tail.",
        choices=("exponential", "hyperbolic"),
    ),
    Knob(
        "atrophy_leader_stretch",
        "time",
        "half-life stretch by p(leader)",
        0,
        1.75,
        0.25,
        "Half-life x (1 + stretch x (p(leader) - 0.5)). 0 = every thread decays alike.",
    ),
    Knob(
        "alive_weight",
        "time",
        "alive weight",
        0,
        3,
        0.1,
        "Lift for long-running threads that are still fresh. 0 = display only.",
    ),
    Knob(
        "alive_k",
        "time",
        "alive k (work h)",
        1,
        24,
        1,
        "Half point of the alive ramp in working hours.",
    ),
    Knob(
        "max_thread_age_days",
        "time",
        "max age (days)",
        1,
        14,
        1,
        "Days of silence after which a thread leaves the board. Can only narrow the yml's.",
        narrows_only=True,
    ),
    Knob(
        "velocity_weight",
        "burst",
        "velocity weight",
        0,
        60,
        1,
        "How much a reply-per-minute rate lifts the score before the cap.",
    ),
    Knob(
        "velocity_window_minutes",
        "burst",
        "window (min)",
        5,
        240,
        5,
        "How far back right now reaches for the burst term.",
    ),
    Knob(
        "activity_cap",
        "burst",
        "activity cap",
        1,
        100,
        1,
        "Ceiling on the burst vote so a flood cannot outrank everything.",
    ),
    Knob(
        "followed_drop",
        "following",
        "drop (1=off)",
        0,
        1,
        0.05,
        "Multiplier on a thread Slack follows for you. 1 = off, 0 = followed threads vanish.",
    ),
    Knob(
        "followed_rebuild_per_msg",
        "following",
        "rebuild per unread msg",
        0,
        1,
        0.05,
        "How fast replies after your Slack last_read restore a followed thread. 0 = stay down.",
    ),
    Knob(
        "leader_weight",
        "topics",
        "needs leader: weight",
        0,
        100,
        5,
        "Vote: + weight x p(needs leader), beside base.",
    ),
    Knob(
        "leader_gate_floor",
        "topics",
        "needs leader: gate floor (1=off)",
        0,
        1,
        0.05,
        "Gate: score x (floor + (1 - floor) x p(leader)).",
    ),
    Knob(
        "resolved_drop",
        "topics",
        "resolved: drop",
        0,
        1,
        0.05,
        "Gate: score x (1 - drop x p(resolved)).",
    ),
    Knob(
        "tier_method",
        "tiers",
        "tier method",
        0,
        0,
        0,
        "relative: top N rows above a floor. absolute: score lines.",
        choices=("relative", "absolute"),
    ),
    Knob(
        "tier_hot_count",
        "tiers",
        "relative: hot count",
        0,
        10,
        1,
        "Relative method: this many top rows paint hot.",
    ),
    Knob(
        "tier_warm_count",
        "tiers",
        "relative: warm count",
        0,
        30,
        1,
        "Relative method: rows up to this rank paint warm.",
    ),
    Knob(
        "tier_floor",
        "tiers",
        "relative: floor",
        0,
        50,
        1,
        "Relative method: below this score a row is never hot or warm.",
    ),
    Knob(
        "tier_hot",
        "tiers",
        "absolute: hot line",
        0,
        200,
        5,
        "Absolute method: hot at or above this score.",
    ),
    Knob(
        "tier_warm",
        "tiers",
        "absolute: warm line",
        0,
        200,
        5,
        "Absolute method: warm at or above this score.",
    ),
)

# Every HeatConfig field that is not a panel knob, with why. The coverage test partitions
# HeatConfig's fields between this and KNOBS, so a new field must be placed on purpose.
NOT_A_VIEW_KNOB: dict[str, str] = {
    "work_window": "a nested model (the working-hours clock), not a scalar",
    "decay_hours": "superseded by atrophy; kept for config compatibility",
    "decay_floor": "superseded by atrophy; kept for config compatibility",
    "hot_threshold": "superseded by tier-hot; kept for config compatibility",
    "warm_threshold": "superseded by tier-warm; kept for config compatibility",
    "retitle_reply_growth": "the LLM retitle rule, not a rank input",
    "retitle_reply_percent": "the LLM retitle rule, not a rank input",
    "channel_weights": "rendered as a map: heat.channel-weights.<channel>",
    "people_weights": "rendered as a map: heat.people-weights.<user-id>",
    "spiking_threshold": "a mark knob, not a rank input",
    "new_window_minutes": "a mark knob, not a rank input",
    "unanswered_enabled": "a mark knob, not a rank input",
    "unanswered_max_replies": "a mark knob, not a rank input",
    "unanswered_min_age_hours": "a mark knob, not a rank input",
    "resurrection_gap_hours": "a mark knob, not a rank input",
    "resurrection_age_days": "a mark knob, not a rank input",
    "resurrection_display_hours": "a mark knob, not a rank input",
    "heated_threshold": "a mark knob, not a rank input",
    "heated_structural_scale": "a mark knob, not a rank input",
    "heated_tone_weight": "a mark knob, not a rank input",
}

_KNOB_BY_QUERY_KEY: dict[str, Knob] = {k.query_key: k for k in KNOBS}

# The panel's sections, in the order the explorer lays them out; weight maps follow.
GROUPS = ("size", "time", "burst", "following", "topics", "tiers")
# A weight-map slider reaches at least this far, wider when the yml already goes past it.
MAP_WEIGHT_HI = 3.0
MAP_WEIGHT_STEP = 0.1


class OverrideError(ValueError):
    """A request's knob overrides were rejected; the message is the response body (422)."""


def parse_overrides(params: Iterable[tuple[str, str]]) -> dict[str, str]:
    """The ``heat.*`` query parameters as a raw ``{query key: value}`` map, last value wins.

    Parameters outside the ``heat.`` namespace (``group-by``, ``compact``) are not overrides
    and are skipped. A ``heat.`` key that is not a knob or a named map entry is a
    ``ValueError``: an unknown key is never silently ignored. Values stay strings here;
    ``apply_overrides`` validates them.
    """
    overrides: dict[str, str] = {}
    for key, value in params:
        if not key.startswith(PREFIX):
            continue
        if key not in _KNOB_BY_QUERY_KEY:
            field, _, name = key.removeprefix(PREFIX).partition(".")
            if field not in MAPS:
                raise ValueError(
                    f"unknown override {key}; known: "
                    f"{', '.join(sorted(_KNOB_BY_QUERY_KEY))}, "
                    f"{', '.join(PREFIX + m + '.<name>' for m in MAPS)}"
                )
            if not name:
                raise ValueError(f"override {key} names no entry; use {PREFIX}{field}.<name>")
        overrides[key] = value
    logger.debug("parse_overrides: overrides=%s", overrides)
    return overrides


def _describe(exc: ValidationError) -> str:
    """One line per pydantic error, located by the query key a viewer would have typed."""
    lines = []
    for err in exc.errors():
        # The field is snake in pydantic's loc, kebab in the query; a map entry's name is raw.
        parts = [str(part) for part in err["loc"]]
        loc = ".".join([p.replace("_", "-") for p in parts[:1]] + parts[1:])
        where = f"{PREFIX}{loc}: " if loc else ""
        lines.append(f"{where}{err['msg']}")
    return "; ".join(lines)


def apply_overrides(config: AppConfig, overrides: dict[str, str]) -> AppConfig:
    """``config`` with ``overrides`` merged over its ``heat`` block, re-validated.

    No overrides returns ``config`` itself, so the no-override render is the yml render. A
    value ``HeatConfig`` rejects, or a narrows-only knob above the yml's value, is a
    ``ValueError`` naming the key; nothing is clamped.
    """
    if not overrides:
        return config
    data: dict[str, Any] = config.heat.model_dump()
    for key, value in overrides.items():
        knob = _KNOB_BY_QUERY_KEY.get(key)
        if knob is not None:
            data[knob.key] = value
            continue
        field, _, name = key.removeprefix(PREFIX).partition(".")
        data[field.replace("-", "_")] = {**data[field.replace("-", "_")], name: value}
    try:
        heat = HeatConfig.model_validate(data)
    except ValidationError as exc:
        raise ValueError(_describe(exc)) from exc
    for knob in KNOBS:
        if not knob.narrows_only:
            continue
        yml = getattr(config.heat, knob.key)
        if getattr(heat, knob.key) > yml:
            raise ValueError(
                f"{knob.query_key} ({getattr(heat, knob.key)}) may not exceed the yml's {yml}: "
                "the poller evicts threads past it, so a larger value cannot bring them back"
            )
    logger.debug("apply_overrides: applied %d overrides", len(overrides))
    return config.model_copy(update={"heat": heat})


@dataclass(frozen=True)
class RequestConfig:
    """The config one request renders with, and the overrides that built it."""

    config: AppConfig
    overrides: dict[str, str]

    @property
    def overrides_json(self) -> str:
        """The overrides as compact JSON for ``data-overrides``; empty when there are none."""
        return json.dumps(self.overrides, separators=(",", ":")) if self.overrides else ""


def request_config(base: AppConfig) -> Callable[[Request], RequestConfig]:
    """A FastAPI dependency building each request's config from ``base`` and its query.

    A rejected override raises ``OverrideError``; ``web.create_routes`` turns that into a 422
    whose body is the message.
    """

    def dependency(request: Request) -> RequestConfig:
        try:
            overrides = parse_overrides(request.query_params.multi_items())
            config = apply_overrides(base, overrides)
        except ValueError as exc:
            logger.info("request_config: rejected %s: %s", request.url.query, exc)
            raise OverrideError(str(exc)) from exc
        return RequestConfig(config, overrides)

    return dependency


def _fmt(value: float) -> str:
    """A number as the panel and the query write it: ``2``, ``0.25``, never ``2.0``."""
    return format(float(value), "g")


@dataclass(frozen=True)
class PanelKnob:
    """One row of the panel: a knob, or one named entry of a weight map, with its yml value."""

    key: str  # the query key (``heat.velocity-weight``), what parse_overrides accepts
    label: str
    help: str
    yml: str  # the yml's value, written as the query would write it
    lo: str = ""
    hi: str = ""
    step: str = ""
    tick: float = 0.0  # where the yml value sits on the slider, percent from the left
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class PanelGroup:
    name: str
    knobs: tuple[PanelKnob, ...]
    dimmed: str = ""  # why the group does nothing right now; empty = live


def _tick(value: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.0
    return round(min(100.0, max(0.0, (value - lo) / (hi - lo) * 100)), 2)


def _panel_knob(knob: Knob, heat: HeatConfig) -> PanelKnob:
    value = getattr(heat, knob.key)
    if knob.choices:
        return PanelKnob(knob.query_key, knob.label, knob.help, str(value), choices=knob.choices)
    # A narrows-only knob above the yml's value is a 422, so the slider stops at the yml.
    hi = min(knob.hi, value) if knob.narrows_only else knob.hi
    return PanelKnob(
        knob.query_key,
        knob.label,
        knob.help,
        _fmt(value),
        _fmt(knob.lo),
        _fmt(hi),
        _fmt(knob.step),
        _tick(value, knob.lo, hi),
    )


def _map_group(field: str, weights: dict[str, float], prefix: str) -> PanelGroup:
    rows = []
    for name in sorted(weights):
        hi = max(MAP_WEIGHT_HI, weights[name])
        rows.append(
            PanelKnob(
                f"{PREFIX}{field}.{name}",
                f"{prefix}{name}",
                f"{field} entry from the yml; an override replaces it for this view",
                _fmt(weights[name]),
                "0",
                _fmt(hi),
                _fmt(MAP_WEIGHT_STEP),
                _tick(weights[name], 0, hi),
            )
        )
    return PanelGroup(field, tuple(rows))


def panel_groups(config: AppConfig) -> list[PanelGroup]:
    """Every knob the panel shows, grouped, with the yml's value; the two weight maps last.

    Rendered from ``KNOBS`` and the yml only, never from a request: the panel shows what the
    yml says, and the viewer's overrides ride on top of it in the browser. The topic group is
    dimmed when ``topics.api-key`` is empty, since no thread carries a probability then.
    """
    groups = []
    for name in GROUPS:
        knobs = tuple(_panel_knob(k, config.heat) for k in KNOBS if k.group == name)
        dimmed = "no rater configured" if name == "topics" and not config.topics.api_key else ""
        groups.append(PanelGroup(name, knobs, dimmed))
    groups.append(_map_group("channel-weights", config.heat.channel_weights, "#"))
    groups.append(_map_group("people-weights", config.heat.people_weights, ""))
    return [g for g in groups if g.knobs]


def shaping_inputs(ranked: list[Ranked], config: HeatConfig, now: float) -> list[dict[str, Any]]:
    """The raw inputs the shaping charts bin, one row per ranked thread, under ``config``.

    ``volume`` feeds base, ``since-last`` (working hours) feeds atrophy, ``p-leader`` feeds the
    vote (``None`` until jev rates the thread), ``score`` is the request's overridden score.
    """
    rows: list[dict[str, Any]] = []
    for r in ranked:
        b = heat_breakdown(r.entry, config, now)
        rows.append(
            {
                "volume": b.message_count * config.reply_weight + b.people_term,
                "since-last": b.time_since_last,
                "p-leader": b.p_leader,
                "score": r.score,
            }
        )
    return rows
