import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict


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


class HeatConfig(_KebabModel):
    reply_weight: int = 2
    participant_weight: int = 3
    decay_half_life_hours: int = 24
    max_thread_age_days: int = 3
    hot_threshold: int = 50
    warm_threshold: int = 20
    retitle_reply_growth: int = 5
    retitle_reply_percent: int = 25


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
    raw = yaml.safe_load(path.read_text()) or {}
    interpolated = _interpolate_recursive(raw)
    return AppConfig.model_validate(interpolated)
