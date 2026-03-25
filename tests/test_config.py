import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from slack_dashboard.config import (
    FetchConfig,
    HeatConfig,
    LlmConfig,
    ServerConfig,
    SlackConfig,
    load_config,
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
            "decay-half-life-hours": 48,
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
    assert config.heat.decay_half_life_hours == 48
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
    heat = HeatConfig()
    assert heat.reply_weight == 2
    assert heat.participant_weight == 3
    assert heat.decay_half_life_hours == 24
    assert heat.max_thread_age_days == 3
    assert heat.hot_threshold == 50
    assert heat.warm_threshold == 20
    assert heat.retitle_reply_growth == 5
    assert heat.retitle_reply_percent == 25
    llm = LlmConfig()
    assert llm.provider == "anthropic"
    assert llm.model == "claude-haiku-4-5-20251001"
    server = ServerConfig()
    assert server.host == "0.0.0.0"
    assert server.port == 8080
    assert server.log_level == "info"
