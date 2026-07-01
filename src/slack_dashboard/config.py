import logging
import os
import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

logger = logging.getLogger(__name__)


def _snake_to_kebab(name: str) -> str:
    return name.replace("_", "-")


class _KebabModel(BaseModel):
    model_config = ConfigDict(alias_generator=_snake_to_kebab, populate_by_name=True)


# Canonical three-letter weekday tokens accepted in work-days, indexed to match
# datetime.weekday() (Monday == 0 ... Sunday == 6).
_WEEKDAY_TOKENS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class WorkWindowConfig(_KebabModel):
    """The working-hours band that atrophy runs on (see worktime.business_hours_between).

    Nested under HeatConfig as ``heat.work-window`` (NOT composed into AppConfig): the
    working-hours clock is a heat-model concern, and pinning it here kills the placement
    ambiguity. Nights, weekends, and any hour outside [start_hour, end_hour) on a work day
    contribute zero working hours.
    """

    timezone: str = "America/Los_Angeles"
    start_hour: int = 6
    end_hour: int = 18
    work_days: list[str] = ["mon", "tue", "wed", "thu", "fri"]

    @model_validator(mode="after")
    def _validate(self) -> "WorkWindowConfig":
        # Fail clearly at boot rather than silently producing a degenerate window.
        if self.end_hour <= self.start_hour:
            raise ValueError(
                f"work-window end-hour ({self.end_hour}) must be greater than "
                f"start-hour ({self.start_hour})"
            )
        if not self.work_days:
            raise ValueError("work-window work-days must not be empty")
        unknown = [d for d in self.work_days if d.lower() not in _WEEKDAY_TOKENS]
        if unknown:
            raise ValueError(
                f"work-window work-days has unknown day tokens {unknown}; "
                f"expected any of {list(_WEEKDAY_TOKENS)}"
            )
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"work-window timezone {self.timezone!r} is not resolvable") from e
        return self

    def work_weekdays(self) -> set[int]:
        """The configured work days as datetime.weekday() integers (Mon == 0 ... Sun == 6)."""
        return {_WEEKDAY_TOKENS.index(d.lower()) for d in self.work_days}

    def tzinfo(self) -> ZoneInfo:
        """The resolved zoneinfo for this window (validated at construction)."""
        return ZoneInfo(self.timezone)


class SlackConfig(_KebabModel):
    token: str = ""
    app_token: str = ""
    team_id: str = ""


class FetchConfig(_KebabModel):
    refresh_interval_minutes: int = 10
    min_replies: int = 3
    channel_min_replies: dict[str, int] = {}


class HeatConfig(_KebabModel):
    reply_weight: int = 2
    participant_weight: int = 3
    decay_hours: int = 24
    decay_floor: float = 0.01
    max_thread_age_days: int = 3
    # Legacy absolute tier thresholds. Retained for backward-compat and migrated onto the
    # new tier-hot / tier-warm knobs (see _migrate_tier_thresholds); no longer read directly.
    hot_threshold: int = 50
    warm_threshold: int = 20
    retitle_reply_growth: int = 5
    retitle_reply_percent: int = 25
    channel_weights: dict[str, float] = {}
    # Per-person importance, keyed by stable Slack user_id (glob-aware). A participant's
    # weight defaults to participant_weight; entries here raise (or lower) specific people so
    # a thread the right people are in floats up. Additive into base, bounded by
    # people_weight_cap. PRIVATE: real ids/weights live only in the user's ~/.config, never
    # in this public repo.
    people_weights: dict[str, float] = {}
    # Cap on the total per-thread people contribution to base (0 = no cap). Bounds a VIP
    # pile-up so a crowd of weighted people cannot run the score away.
    people_weight_cap: float = 0.0
    velocity_weight: float = 0.0
    velocity_window_minutes: int = 30
    # Replies-in-window count that marks a thread as spiking (⚡). Aligned with the
    # velocity group-by "spiking (15+)" tier so the glyph and grouping agree.
    spiking_threshold: int = 15
    # Minutes after first observation that the ✨ new glyph stays visible. 60 min
    # is long enough to survive a coffee-break gap, short enough not to read as stale.
    new_window_minutes: int = 60
    # Unanswered proxy glyph (❓): arithmetic proxy for a dropped-ball question.
    # Ships disabled by default (unanswered-enabled: false); flip on in private ~/.config
    # to observe on Monday. The proxy fires when the first message contains "?" AND
    # message_count is at or below unanswered-max-replies AND the thread is older than
    # unanswered-min-age-hours. Effective only in ops channels running channel-min-replies:
    # 1 (e.g. sre, ask-security); standard channels require 3+ replies to appear so
    # max-replies: 3 never fires there.
    # NOTE: default bumped from 2 to 3 because message_count now includes the root message
    # (previously reply_count excluded it).  A no-reply question is message_count == 1;
    # max-replies: 3 preserves the old "root + up to 2 replies" firing window.
    unanswered_enabled: bool = False
    unanswered_max_replies: int = 3
    unanswered_min_age_hours: int = 2
    resurrection_gap_hours: int = 24
    resurrection_age_days: int = 2
    resurrection_display_hours: int = 24
    # Heated-exchange signal config (the 🌶️ pepper glyph).
    # heated_score = structural_term + tone_term; 🌶️ fires when >= heated_threshold.
    # structural_term: exchange-gated intensity, clamped 0-10, floor-free decay.
    # tone_term: heated_tone (0-3) * heated_tone_weight = 0..9 (Phase 2).
    heated_threshold: float = 8.0
    heated_structural_scale: float = 1.0
    heated_tone_weight: float = 3.0

    # Working-hours band the atrophy clock runs on (heat.work-window). Nights and weekends
    # contribute zero working hours, so a Friday-afternoon thread does not go stone-cold
    # over the weekend. Consumed by worktime.business_hours_between via heat_breakdown;
    # nested here (not on AppConfig) since it is a heat-model concern.
    work_window: WorkWindowConfig = WorkWindowConfig()

    # ---- Re-shaped score knobs (heat_breakdown, single arithmetic path) ----
    # atrophy = 0.5 ** (time_since_last_work_hours / atrophy_half_life_work_hours). A thread
    # idle 3 work-hours is at 0.5; idle ~12 work-hours (>1 working day) is ~0.06 -> cold.
    atrophy_half_life_work_hours: float = 3.0
    # base_norm = base_cap * volume / (volume + base_k): a HARD asymptotic ceiling (-> base_cap
    # as volume -> inf), monotone, so a huge stale thread cannot dominate a small fresh one.
    # Seeds: base_cap 50 keeps the historical scale roughly intact (a well-populated thread
    # approaches ~50, the old absolute hot line); base_k 15 places the half-saturation point
    # near volume 15 (~a handful of messages + a couple people), so the curve bends within the
    # everyday message-count range rather than staying near-linear. Calibration (Phase 4) tunes.
    base_cap: float = 50.0
    base_k: float = 15.0
    # activity = min(activity_cap, velocity * velocity_weight): a bounded additive burst term
    # kept OUTSIDE the volume ceiling. Seed cap 20 = a strong spike can add up to ~40% of a
    # saturated base_norm without letting velocity run the score away. velocity_weight defaults
    # 0.0 in code (the live ~/.config sets 5.0), so activity is 0 until velocity is weighted.
    activity_cap: float = 20.0
    # alive_boost = 1 + alive_weight * f(time_alive) * atrophy; f = time_alive/(time_alive+alive_k).
    # alive_weight 0.0 seed = time-alive is DISPLAY-ONLY until calibration says otherwise (per the
    # design doc). alive_k 6.0 places f's half-point near 6 work-hours (~a full working day of
    # life) so the longevity lift ramps over a realistic incident lifetime, not instantly.
    alive_weight: float = 0.0
    alive_k: float = 6.0
    # Drop-and-rebuild involvement (replaces involved_damping/involved_decay_*). Posting drops
    # the score to involved_drop (0.8 -> a 20% cut, a bigger initial drop than the old 0.5-cut
    # default), then each unseen reply after the user's last post rebuilds toward 1.0 at rate
    # involved_rebuild_per_msg (0.15 -> ~7 unseen messages fully restores). Clamped to
    # [involved_drop, 1]. involved_drop = 1.0 disables the feature (no drop).
    involved_drop: float = 0.8
    involved_rebuild_per_msg: float = 0.15
    # Tiering (see heat.classify_tier). tier_method: "absolute" (score thresholds) or "relative"
    # (rank-aware top-N with an absolute floor). Seed: relative mode is the DEFAULT - the
    # calibration arena (Phase 4, docs/design/2026-06-30-calibration-trace.md) found it is the
    # one decisive knob that takes the busy board from 3/7 to 7/7 criteria passing over the
    # Phase 2 seed shapes, because it is rank-aware where absolute mode is not: a bounded
    # score against a fixed absolute line still lets a busy-but-idle thread crowd out fresher,
    # smaller ones. tier_hot 50 / tier_warm 20 mirror the historical hot/warm lines and remain
    # meaningful in absolute mode because base_norm is ceilinged. tier_hot_count 3 /
    # tier_warm_count 10 = relative-mode top-N sizes. tier_floor 5.0 = the absolute floor in
    # relative mode that keeps a fully-atrophied board from painting top-N hot; a thread
    # scoring below 5 is never hot/warm even if top-ranked.
    tier_method: str = "relative"
    tier_hot: float = 50.0
    tier_warm: float = 20.0
    tier_hot_count: int = 3
    tier_warm_count: int = 10
    tier_floor: float = 5.0

    @model_validator(mode="before")
    @classmethod
    def _migrate_decay_half_life(cls, data: Any) -> Any:
        # Backward-compat: the old config key was decay-half-life-hours; the math was
        # always a linear ramp, not a half-life. Map it onto decay-hours when the new
        # key is absent so existing config files keep loading.
        if not isinstance(data, dict):
            return data
        for legacy in ("decay-half-life-hours", "decay_half_life_hours"):
            if legacy in data and "decay-hours" not in data and "decay_hours" not in data:
                logger.debug("Migrating legacy config key %s -> decay-hours", legacy)
                data["decay-hours"] = data[legacy]
        return data

    @model_validator(mode="before")
    @classmethod
    def _migrate_tier_thresholds(cls, data: Any) -> Any:
        # Backward-compat: the absolute tier thresholds were hot-threshold / warm-threshold.
        # Map them onto the new tier-hot / tier-warm knobs when the new keys are absent so
        # existing config files keep loading (mirrors _migrate_decay_half_life).
        if not isinstance(data, dict):
            return data
        for legacy, new in (("hot-threshold", "tier-hot"), ("warm-threshold", "tier-warm")):
            legacy_snake = legacy.replace("-", "_")
            new_snake = new.replace("-", "_")
            has_new = new in data or new_snake in data
            if not has_new and (legacy in data or legacy_snake in data):
                value = data.get(legacy, data.get(legacy_snake))
                logger.debug("Migrating legacy config key %s -> %s", legacy, new)
                data[new] = value
        return data

    @model_validator(mode="before")
    @classmethod
    def _warn_deprecated_involved_keys(cls, data: Any) -> Any:
        # The old involvement knobs (involved-damping / involved-decay-messages /
        # involved-decay-hours) were replaced by involved-drop / involved-rebuild-per-msg when
        # the involvement model changed from exponential decay to drop-and-rebuild. There is NO
        # clean 1:1 value migration (the model changed shape), so unlike _migrate_tier_thresholds
        # we do NOT map a value - we WARN and proceed with the new defaults. extra keys are
        # ignored by the model (ConfigDict has no extra="forbid"), so without this warning an
        # existing private config with the old keys would silently revert to defaults.
        if not isinstance(data, dict):
            return data
        deprecated = {
            "involved-damping": "involved-drop / involved-rebuild-per-msg",
            "involved-decay-messages": "involved-rebuild-per-msg",
            "involved-decay-hours": "involved-rebuild-per-msg",
        }
        for legacy, replacement in deprecated.items():
            legacy_snake = legacy.replace("-", "_")
            if legacy in data or legacy_snake in data:
                logger.warning(
                    "heat config key %r is deprecated and IGNORED; the involvement model is now "
                    "drop-and-rebuild - set %s instead (defaults are in effect until you do)",
                    legacy,
                    replacement,
                )
        return data

    @model_validator(mode="after")
    def _validate_tier_method(self) -> "HeatConfig":
        if self.tier_method not in ("absolute", "relative"):
            raise ValueError(
                f"heat tier-method {self.tier_method!r} must be 'absolute' or 'relative'"
            )
        return self

    @model_validator(mode="after")
    def _validate_score_bounds(self) -> "HeatConfig":
        # Fail clearly at boot (not with a ZeroDivisionError deep in heat_breakdown) when a
        # re-model knob is out of range. base_k / atrophy_half_life_work_hours / alive_k are
        # denominators, so a zero would divide-by-zero at scoring time; base_cap / activity_cap
        # being <= 0 would silently zero the score. alive_weight MAY be 0 (display-only seed),
        # so it is >= 0, not > 0.
        positive = (
            ("base-cap", self.base_cap),
            ("base-k", self.base_k),
            ("activity-cap", self.activity_cap),
            ("atrophy-half-life-work-hours", self.atrophy_half_life_work_hours),
            ("alive-k", self.alive_k),
        )
        for name, value in positive:
            if value <= 0:
                raise ValueError(f"heat {name} ({value}) must be greater than 0")
        if self.alive_weight < 0:
            raise ValueError(
                f"heat alive-weight ({self.alive_weight}) must be greater than or equal to 0"
            )
        return self


class LlmConfig(_KebabModel):
    provider: str = "anthropic"
    model: str = "claude-haiku-4-5-20251001"
    api_key: str = ""


class ServerConfig(_KebabModel):
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "info"


class DisplayConfig(_KebabModel):
    # Compact view fold: the default pane renders the top compact_rows threads by heat
    # ("one page worth"); the rest are still server-rendered into the DOM but hidden via
    # CSS until the global show-all toggle is flipped (zero-miss disclosure, see design
    # doc Triage v3). 0 disables the fold (show everything at rest).
    compact_rows: int = 20


class AppConfig(_KebabModel):
    slack: SlackConfig = SlackConfig()
    channels: dict[str, str] = {}
    fetch: FetchConfig = FetchConfig()
    heat: HeatConfig = HeatConfig()
    llm: LlmConfig = LlmConfig()
    server: ServerConfig = ServerConfig()
    display: DisplayConfig = DisplayConfig()
    workspace: str = ""


def _resolve_glob(name: str, mapping: dict[str, Any], default: Any) -> Any:
    """Resolve a per-channel override by name.

    Exact keys win over globs; among globs, the first match in sorted-key order
    is returned (deterministic when multiple globs match). Falls back to default.
    """
    if name in mapping:
        return mapping[name]
    for key in sorted(mapping):
        if fnmatch(name, key):
            return mapping[key]
    return default


def resolve_channel_weight(name: str, config: HeatConfig) -> float:
    weight: float = _resolve_glob(name, config.channel_weights, 1.0)
    logger.debug("resolve_channel_weight: name=%s weight=%s", name, weight)
    return weight


def resolve_person_weight(user_id: str, config: HeatConfig) -> float:
    """Per-person weight by Slack user_id; defaults to participant_weight when unlisted."""
    default = float(config.participant_weight)
    weight: float = _resolve_glob(user_id, config.people_weights, default)
    logger.debug("resolve_person_weight: user_id=%s weight=%s", user_id, weight)
    return weight


def resolve_min_replies(name: str, config: FetchConfig) -> int:
    value: int = _resolve_glob(name, config.channel_min_replies, config.min_replies)
    logger.debug("resolve_min_replies: name=%s min_replies=%s", name, value)
    return value


_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _interpolate_env(value: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))

    return _ENV_VAR_RE.sub(_replace, value)


def _interpolate_recursive(data: object) -> object:
    if isinstance(data, str):
        return _interpolate_env(data)
    if isinstance(data, dict):
        return {k: _interpolate_recursive(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_interpolate_recursive(item) for item in data]
    return data


def load_config(path: Path) -> AppConfig:
    logger.debug("load_config: path=%s", path)
    raw = yaml.safe_load(path.read_text()) or {}
    interpolated = _interpolate_recursive(raw)
    config = AppConfig.model_validate(interpolated)
    logger.debug(
        "load_config: loaded channels=%d workspace=%s",
        len(config.channels),
        config.workspace,
    )
    return config
