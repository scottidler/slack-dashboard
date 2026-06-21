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
    velocity_weight: float = 0.0
    velocity_window_minutes: int = 30
    resurrection_gap_hours: int = 24
    resurrection_age_days: int = 2
    resurrection_display_hours: int = 24

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


class AppConfig(_KebabModel):
    slack: SlackConfig = SlackConfig()
    channels: dict[str, str] = {}
    fetch: FetchConfig = FetchConfig()
    heat: HeatConfig = HeatConfig()
    llm: LlmConfig = LlmConfig()
    server: ServerConfig = ServerConfig()
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
