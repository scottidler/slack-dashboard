import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from slack_dashboard.config import (
    DEFAULT_MARKS,
    MARK_STATES,
    AppConfig,
    DisplayConfig,
    FetchConfig,
    HeatConfig,
    LlmConfig,
    ServerConfig,
    SlackConfig,
    load_config,
    require_secrets,
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
    # Re-shaped score knobs (Phase 2 seed defaults).
    assert heat.atrophy_half_life_work_hours == 3.0
    assert heat.base_cap == 50.0
    assert heat.base_k == 15.0
    assert heat.activity_cap == 20.0
    assert heat.alive_weight == 0.0
    assert heat.alive_k == 6.0
    assert heat.followed_drop == 0.8
    assert heat.followed_rebuild_per_msg == 0.15
    # tier-method default is "relative": the calibration arena (Phase 4/5, see
    # docs/design/2026-06-30-calibration-trace.md) found flipping this one knob over the
    # Phase 2 seed shapes is the decisive change taking the busy board from 3/7 to 7/7
    # criteria passing.
    assert heat.tier_method == "relative"
    assert heat.tier_hot == 50.0
    assert heat.tier_warm == 20.0
    assert heat.tier_hot_count == 3
    assert heat.tier_warm_count == 10
    assert heat.tier_floor == 5.0
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


def test_decay_hours_with_legacy_key_is_an_error(tmp_path: Path) -> None:
    # Was "decay-hours wins over legacy": the legacy key rode along silently ignored. With
    # extra="forbid" a leftover legacy key beside its replacement fails at boot instead.
    data = {
        "channels": {"general": "C111"},
        "heat": {"decay-hours": 12, "decay-half-life-hours": 48},
    }
    config_file = write_config(tmp_path, data)
    with pytest.raises(ValidationError, match="decay-half-life-hours"):
        load_config(config_file)


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


@pytest.mark.parametrize(
    "field",
    ["base_cap", "base_k", "activity_cap", "atrophy_half_life_work_hours", "alive_k"],
)
def test_positive_score_knob_rejects_zero_at_construction(field: str) -> None:
    # These knobs must fail clearly at config-load, not with a ZeroDivisionError deep in
    # heat_breakdown (base_k / atrophy_half_life_work_hours / alive_k are denominators).
    with pytest.raises(ValueError):
        HeatConfig(**{field: 0.0})


@pytest.mark.parametrize(
    "field",
    ["base_cap", "base_k", "activity_cap", "atrophy_half_life_work_hours", "alive_k"],
)
def test_positive_score_knob_rejects_negative_at_construction(field: str) -> None:
    with pytest.raises(ValueError):
        HeatConfig(**{field: -1.0})


def test_alive_weight_zero_is_allowed() -> None:
    # alive_weight == 0.0 is the shipped display-only seed; it must NOT be over-constrained.
    assert HeatConfig(alive_weight=0.0).alive_weight == 0.0


def test_alive_weight_negative_rejected() -> None:
    with pytest.raises(ValueError):
        HeatConfig(alive_weight=-0.5)


@pytest.mark.parametrize(
    ("legacy", "replacement"),
    [
        ("involved-drop", "followed-drop"),
        ("involved-rebuild-per-msg", "followed-rebuild-per-msg"),
        ("involved-damping", "followed-drop"),
        ("involved-decay-messages", "followed-rebuild-per-msg"),
        ("involved-decay-hours", "followed-rebuild-per-msg"),
    ],
)
def test_retired_involved_keys_are_errors_naming_the_replacement(
    tmp_path: Path, legacy: str, replacement: str
) -> None:
    # Was a warning that ran on defaults; a renamed key now fails at boot and names its
    # replacement, so the fix is one edit.
    config_file = write_config(tmp_path, {"heat": {legacy: 0.5}})
    with pytest.raises(ValidationError, match=replacement):
        load_config(config_file)


@pytest.mark.parametrize("legacy", ["involved-drop", "involved-rebuild-per-msg"])
def test_retired_involved_keys_rejected_in_snake_case(legacy: str) -> None:
    # populate_by_name accepts snake keys, so the retired names are caught in either spelling.
    with pytest.raises(ValidationError, match="removed"):
        HeatConfig.model_validate({legacy.replace("-", "_"): 0.5})


def test_unknown_key_rejected_everywhere(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="bogus"):
        HeatConfig.model_validate({"bogus": 1})
    with pytest.raises(ValidationError, match="bogus"):
        load_config(write_config(tmp_path, {"fetch": {"bogus": 1}}))
    with pytest.raises(ValidationError, match="bogus"):
        load_config(write_config(tmp_path, {"bogus": 1}))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_heat_rejects_nan_and_inf(value: float) -> None:
    with pytest.raises(ValidationError):
        HeatConfig(base_cap=value)
    with pytest.raises(ValidationError):
        HeatConfig(channel_weights={"sre": value})
    with pytest.raises(ValidationError):
        HeatConfig(people_weights={"U1": value})


@pytest.mark.parametrize(
    "field",
    [
        "reply_weight",
        "participant_weight",
        "velocity_weight",
        "people_weight_cap",
        "followed_drop",
        "followed_rebuild_per_msg",
        "tier_floor",
        "max_thread_age_days",
    ],
)
def test_heat_rejects_negative_weights_caps_and_counts(field: str) -> None:
    with pytest.raises(ValidationError, match="must be >= 0"):
        HeatConfig.model_validate({field: -1})


def test_heat_rejects_negative_map_weights() -> None:
    with pytest.raises(ValidationError, match="channel-weights.sre"):
        HeatConfig(channel_weights={"sre": -1.0})
    with pytest.raises(ValidationError, match="people-weights.U1"):
        HeatConfig(people_weights={"U1": -0.5})


def test_heat_allows_negative_leader_stretch() -> None:
    # The one signed knob: a negative stretch shortens a leader thread's half-life.
    assert HeatConfig(atrophy_leader_stretch=-1.0).atrophy_leader_stretch == -1.0


def test_heat_accepts_zero_weights() -> None:
    config = HeatConfig(followed_drop=0.0, followed_rebuild_per_msg=0.0, velocity_weight=0.0)
    assert config.followed_drop == 0.0


def test_require_secrets_rejects_unresolved_placeholder(tmp_path: Path) -> None:
    # An unset env var leaves the literal ${VAR}; it must not ship as the API key.
    data = {
        "slack": {"token": "xoxp-set"},
        "llm": {"api-key": "${SLACK_DASHBOARD_TEST_MISSING_KEY}"},
    }
    config = load_config(write_config(tmp_path, data))
    with pytest.raises(ValueError, match="llm.api-key: env var SLACK_DASHBOARD_TEST_MISSING_KEY"):
        require_secrets(config)


def test_require_secrets_rejects_empty() -> None:
    config = AppConfig(slack=SlackConfig(token="xoxp-set"), llm=LlmConfig(api_key=""))
    with pytest.raises(ValueError, match="llm.api-key must not be empty"):
        require_secrets(config)


def test_require_secrets_rejects_unresolved_slack_token() -> None:
    config = AppConfig(slack=SlackConfig(token="${UNSET}"), llm=LlmConfig(api_key="sk-set"))
    with pytest.raises(ValueError, match="slack.token: env var UNSET"):
        require_secrets(config)


def test_require_secrets_accepts_resolved() -> None:
    config = AppConfig(slack=SlackConfig(token="xoxp-set"), llm=LlmConfig(api_key="sk-set"))
    require_secrets(config)


# --- Topic knobs and the jev key (docs/design/2026-10-18-heat-autoresearch.md) ----------


def test_topic_knobs_default_neutral() -> None:
    config = HeatConfig()
    assert config.atrophy_curve == "exponential"
    assert config.atrophy_leader_stretch == 0.0
    assert config.leader_weight == 0.0
    assert config.leader_gate_floor == 1.0
    assert config.resolved_drop == 0.0
    assert not config.uses_topics()


@pytest.mark.parametrize(
    "knob",
    [
        {"leader_weight": 45.0},
        {"leader_gate_floor": 0.3},
        {"resolved_drop": 1.0},
        {"atrophy_leader_stretch": 1.0},
    ],
)
def test_uses_topics_when_any_knob_reads_the_probabilities(knob: dict[str, float]) -> None:
    assert HeatConfig(**knob).uses_topics()


def test_atrophy_curve_rejects_an_unknown_shape() -> None:
    with pytest.raises(ValueError, match="atrophy-curve"):
        HeatConfig(atrophy_curve="linear")


@pytest.mark.parametrize(
    "knob, message",
    [
        ({"leader_gate_floor": 1.5}, "leader-gate-floor"),
        ({"resolved_drop": -0.1}, "resolved-drop"),
        ({"leader_weight": -1.0}, "leader-weight"),
        ({"atrophy_leader_stretch": 2.0}, "atrophy-leader-stretch"),
    ],
)
def test_topic_knob_bounds(knob: dict[str, float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        HeatConfig(**knob)


def test_require_secrets_needs_the_jev_key_when_a_topic_knob_is_on() -> None:
    config = AppConfig(
        slack=SlackConfig(token="xoxp-set"),
        llm=LlmConfig(api_key="sk-set"),
        heat=HeatConfig(resolved_drop=1.0),
    )
    with pytest.raises(ValueError, match="topics.api-key must not be empty"):
        require_secrets(config)
    config.topics.api_key = "ts-set"
    require_secrets(config)


def test_require_secrets_ignores_the_jev_key_when_no_knob_reads_it() -> None:
    config = AppConfig(slack=SlackConfig(token="xoxp-set"), llm=LlmConfig(api_key="sk-set"))
    assert config.topics.api_key == ""
    require_secrets(config)


def test_topic_keys_load_in_kebab_case(tmp_path: Path) -> None:
    data = {
        "heat": {
            "atrophy-curve": "hyperbolic",
            "atrophy-leader-stretch": 1.0,
            "leader-weight": 45,
            "leader-gate-floor": 0.3,
            "resolved-drop": 1.0,
        },
        "topics": {"api-key": "ts-set", "model": "jev-1.13.0"},
    }
    config = load_config(write_config(tmp_path, data))
    assert config.heat.atrophy_curve == "hyperbolic"
    assert config.heat.leader_weight == 45.0
    assert config.heat.leader_gate_floor == 0.3
    assert config.heat.resolved_drop == 1.0
    assert config.topics.api_key == "ts-set"
    assert config.topics.model == "jev-1.13.0"
    assert config.topics.endpoint.startswith("https://api.typesafe.ai/")


def test_require_secrets_rejects_an_unresolved_jev_placeholder_even_with_knobs_off() -> None:
    config = AppConfig(slack=SlackConfig(token="xoxp-set"), llm=LlmConfig(api_key="sk-set"))
    config.topics.api_key = "${TYPESAFE_TEST_MISSING}"
    with pytest.raises(ValueError, match="topics.api-key: env var TYPESAFE_TEST_MISSING"):
        require_secrets(config)


# --- display.marks ------------------------------------------------------------------------


def test_display_marks_default_to_every_slot_in_order() -> None:
    glyphs = DisplayConfig().mark_glyphs()
    assert list(glyphs) == list(MARK_STATES)
    assert glyphs == DEFAULT_MARKS
    assert glyphs["new"] == "\N{SPARKLES}"


def test_display_marks_override_one_slot_and_keep_the_rest() -> None:
    glyphs = DisplayConfig(marks={"leader": "\N{SCALES}"}).mark_glyphs()
    assert glyphs["leader"] == "\N{SCALES}"
    assert glyphs["access"] == DEFAULT_MARKS["access"]
    assert list(glyphs) == list(MARK_STATES)


def test_display_marks_reject_unknown_key_and_empty_glyph() -> None:
    with pytest.raises(ValueError, match="unknown keys"):
        DisplayConfig(marks={"bogus": "x"})
    with pytest.raises(ValueError, match="empty glyphs"):
        DisplayConfig(marks={"vip": " "})


def test_display_marks_load_from_kebab_yml(tmp_path: Path) -> None:
    path = tmp_path / "slack-dashboard.yml"
    path.write_text(
        "display:\n  compact-rows: 5\n  marks:\n    following: '\N{WAVING HAND SIGN}'\n"
    )
    config = load_config(path)
    assert config.display.compact_rows == 5
    assert config.display.mark_glyphs()["following"] == "\N{WAVING HAND SIGN}"


def test_display_marks_default_following_is_bell() -> None:
    assert DisplayConfig().mark_glyphs()["following"] == "\N{BELL}"
    assert "me" not in MARK_STATES


def test_display_marks_me_is_an_error_naming_following() -> None:
    with pytest.raises(ValidationError, match="display.marks.following"):
        DisplayConfig(marks={"me": "\N{WAVING HAND SIGN}"})
