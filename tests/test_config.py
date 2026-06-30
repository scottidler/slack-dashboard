import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from slack_dashboard.config import (
    AppConfig,
    FetchConfig,
    HeatConfig,
    LlmConfig,
    ServerConfig,
    SlackConfig,
    load_config,
    resolve_channel_weight,
    resolve_min_replies,
)


def write_config(tmp_path: Path, data: dict[str, Any]) -> Path:
    config_dir = tmp_path / "slack-dashboard"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "slack-dashboard.yml"
    config_file.write_text(yaml.dump(data))
    return config_file


def test_load_minimal_config(tmp_path: Path) -> None:
    data = {
        "channels": {"general": "C111", "random": "C222"},
    }
    config_file = write_config(tmp_path, data)
    config = load_config(config_file)
    assert config.channels == {"general": "C111", "random": "C222"}
    assert config.server.port == 8080
    assert config.heat.reply_weight == 2
    assert config.fetch.refresh_interval_minutes == 10


def test_load_full_config(tmp_path: Path) -> None:
    data = {
        "slack": {"token": "xoxp-test", "app-token": "xapp-test"},
        "channels": {"sre-internal": "C111", "data-platform": "C222"},
        "fetch": {
            "refresh-interval-minutes": 5,
            "min-replies": 5,
        },
        "heat": {
            "reply-weight": 3,
            "participant-weight": 5,
            "decay-hours": 48,
            "max-thread-age-days": 7,
            "hot-threshold": 80,
            "warm-threshold": 30,
            "retitle-reply-growth": 10,
            "retitle-reply-percent": 50,
        },
        "llm": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "api-key": "sk-test",
        },
        "server": {
            "host": "127.0.0.1",
            "port": 9090,
            "log-level": "debug",
        },
    }
    config_file = write_config(tmp_path, data)
    config = load_config(config_file)
    assert config.slack.token == "xoxp-test"
    assert config.slack.app_token == "xapp-test"
    assert config.channels == {"sre-internal": "C111", "data-platform": "C222"}
    assert config.fetch.refresh_interval_minutes == 5
    assert config.fetch.min_replies == 5
    assert config.heat.reply_weight == 3
    assert config.heat.participant_weight == 5
    assert config.heat.decay_hours == 48
    assert config.heat.max_thread_age_days == 7
    assert config.heat.hot_threshold == 80
    assert config.heat.retitle_reply_growth == 10
    assert config.llm.provider == "anthropic"
    assert config.llm.model == "claude-sonnet-4-6"
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 9090
    assert config.server.log_level == "debug"


def test_env_var_interpolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-from-env")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    data = {
        "slack": {"token": "${SLACK_USER_TOKEN}"},
        "channels": {"general": "C111"},
        "llm": {"api-key": "${ANTHROPIC_API_KEY}"},
    }
    config_file = write_config(tmp_path, data)
    config = load_config(config_file)
    assert config.slack.token == "xoxp-from-env"
    assert config.llm.api_key == "sk-from-env"


def test_env_var_not_set_leaves_placeholder(tmp_path: Path) -> None:
    env_key = "SLACK_DASHBOARD_TEST_MISSING_VAR"
    if env_key in os.environ:
        del os.environ[env_key]
    data = {
        "slack": {"token": "${SLACK_DASHBOARD_TEST_MISSING_VAR}"},
        "channels": {"general": "C111"},
    }
    config_file = write_config(tmp_path, data)
    config = load_config(config_file)
    assert config.slack.token == "${SLACK_DASHBOARD_TEST_MISSING_VAR}"


def test_defaults() -> None:
    slack = SlackConfig()
    assert slack.token == ""
    assert slack.app_token == ""
    fetch = FetchConfig()
    assert fetch.refresh_interval_minutes == 10
    assert fetch.min_replies == 3
    assert fetch.channel_min_replies == {}
    heat = HeatConfig()
    assert heat.reply_weight == 2
    assert heat.participant_weight == 3
    assert heat.decay_hours == 24
    assert heat.decay_floor == 0.01
    assert heat.channel_weights == {}
    assert heat.velocity_weight == 0.0
    assert heat.velocity_window_minutes == 30
    assert heat.spiking_threshold == 15
    assert heat.new_window_minutes == 60
    assert heat.unanswered_enabled is False
    assert heat.unanswered_max_replies == 3
    assert heat.unanswered_min_age_hours == 2
    assert heat.resurrection_gap_hours == 24
    assert heat.resurrection_age_days == 2
    assert heat.resurrection_display_hours == 24
    assert heat.max_thread_age_days == 3
    assert heat.hot_threshold == 50
    assert heat.warm_threshold == 20
    assert heat.retitle_reply_growth == 5
    assert heat.retitle_reply_percent == 25
    assert heat.involved_damping == 0.5
    assert heat.involved_decay_messages == 10
    assert heat.involved_decay_hours == 24.0
    llm = LlmConfig()
    assert llm.provider == "anthropic"
    assert llm.model == "claude-haiku-4-5-20251001"
    server = ServerConfig()
    assert server.host == "0.0.0.0"
    assert server.port == 8080
    assert server.log_level == "info"


def test_decay_half_life_backward_compat(tmp_path: Path) -> None:
    data = {
        "channels": {"general": "C111"},
        "heat": {"decay-half-life-hours": 48},
    }
    config_file = write_config(tmp_path, data)
    config = load_config(config_file)
    assert config.heat.decay_hours == 48


def test_decay_hours_wins_over_legacy(tmp_path: Path) -> None:
    data = {
        "channels": {"general": "C111"},
        "heat": {"decay-hours": 12, "decay-half-life-hours": 48},
    }
    config_file = write_config(tmp_path, data)
    config = load_config(config_file)
    assert config.heat.decay_hours == 12


def test_workspace_config(tmp_path: Path) -> None:
    data = {"channels": {"general": "C111"}, "workspace": "tatari"}
    config_file = write_config(tmp_path, data)
    config = load_config(config_file)
    assert config.workspace == "tatari"
    assert AppConfig().workspace == ""


def test_resolve_channel_weight_exact() -> None:
    config = HeatConfig(channel_weights={"sre": 2.0, "proj-*": 0.5})
    assert resolve_channel_weight("sre", config) == 2.0


def test_resolve_channel_weight_glob() -> None:
    config = HeatConfig(channel_weights={"sre": 2.0, "proj-*": 0.5})
    assert resolve_channel_weight("proj-atlas", config) == 0.5


def test_resolve_channel_weight_exact_wins_over_glob() -> None:
    config = HeatConfig(channel_weights={"proj-*": 0.5, "proj-atlas": 3.0})
    assert resolve_channel_weight("proj-atlas", config) == 3.0


def test_resolve_channel_weight_default() -> None:
    config = HeatConfig(channel_weights={"sre": 2.0})
    assert resolve_channel_weight("random", config) == 1.0


def test_resolve_min_replies_default_global() -> None:
    config = FetchConfig(min_replies=3, channel_min_replies={"incidents": 1})
    assert resolve_min_replies("random", config) == 3


def test_resolve_min_replies_override() -> None:
    config = FetchConfig(min_replies=3, channel_min_replies={"incidents": 1})
    assert resolve_min_replies("incidents", config) == 1


def test_resolve_min_replies_glob() -> None:
    config = FetchConfig(min_replies=3, channel_min_replies={"ask-*": 1})
    assert resolve_min_replies("ask-security", config) == 1
