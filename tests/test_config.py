import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from slack_dashboard.config import (
    HeatConfig,
    LlmConfig,
    PollingConfig,
    PruningConfig,
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
    assert config.polling.hot_interval_seconds == 30


def test_load_full_config(tmp_path: Path) -> None:
    data = {
        "slack": {"token": "xoxp-test"},
        "channels": {"sre-internal": "C111", "data-platform": "C222"},
        "polling": {
            "hot-interval-seconds": 15,
            "warm-interval-seconds": 60,
            "cold-interval-seconds": 180,
            "cold-threshold-minutes": 30,
        },
        "heat": {
            "reply-weight": 3,
            "participant-weight": 5,
            "recency-max-bonus": 200,
            "hot-threshold": 80,
            "warm-threshold": 30,
            "retitle-reply-growth": 10,
            "retitle-reply-percent": 50,
        },
        "pruning": {"cold-max-hours": 48},
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
    assert config.channels == {"sre-internal": "C111", "data-platform": "C222"}
    assert config.polling.hot_interval_seconds == 15
    assert config.polling.cold_threshold_minutes == 30
    assert config.heat.reply_weight == 3
    assert config.heat.participant_weight == 5
    assert config.heat.hot_threshold == 80
    assert config.heat.retitle_reply_growth == 10
    assert config.pruning.cold_max_hours == 48
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
    polling = PollingConfig()
    assert polling.hot_interval_seconds == 30
    assert polling.warm_interval_seconds == 120
    assert polling.cold_interval_seconds == 300
    assert polling.cold_threshold_minutes == 60
    heat = HeatConfig()
    assert heat.reply_weight == 2
    assert heat.participant_weight == 3
    assert heat.recency_max_bonus == 100
    assert heat.hot_threshold == 50
    assert heat.warm_threshold == 20
    assert heat.retitle_reply_growth == 5
    assert heat.retitle_reply_percent == 25
    pruning = PruningConfig()
    assert pruning.cold_max_hours == 24
    llm = LlmConfig()
    assert llm.provider == "anthropic"
    assert llm.model == "claude-haiku-4-5-20251001"
    server = ServerConfig()
    assert server.host == "0.0.0.0"
    assert server.port == 8080
    assert server.log_level == "info"
