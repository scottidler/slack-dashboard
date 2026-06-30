import logging
import os
import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

logger = logging.getLogger(__name__)


def _snake_to_kebab(name: str) -> str:
    return name.replace("_", "-")


class _KebabModel(BaseModel):
    model_config = ConfigDict(alias_generator=_snake_to_kebab, populate_by_name=True)


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
