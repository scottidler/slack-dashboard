"""The knob panel: GET /knobs renders every KNOBS row and weight-map entry with the yml's value."""

import re
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import slack_dashboard.web as web_mod
from slack_dashboard.config import AppConfig, HeatConfig, TopicsConfig
from slack_dashboard.knobs import KNOBS, MAPS, panel_groups, parse_overrides
from slack_dashboard.llm.provider import LlmProvider


def _config(api_key: str = "") -> AppConfig:
    return AppConfig(
        heat=HeatConfig(
            velocity_weight=30,
            followed_drop=0.2,
            atrophy_curve="hyperbolic",
            channel_weights={"sre-internal": 2.0, "big": 5.0},
            people_weights={"U0AAA": 1.5},
        ),
        topics=TopicsConfig(api_key=api_key),
    )


def _client(config: AppConfig) -> TestClient:
    app = FastAPI()
    web_mod.create_routes(app, MagicMock(), MagicMock(spec=LlmProvider), config)
    return TestClient(app)


def _rows(html: str) -> dict[str, dict[str, str]]:
    """Each knob row's data attributes, keyed by its query key."""
    rows = {}
    for tag in re.findall(r'<div class="knob" ([^>]*)>', html):
        attrs = dict(re.findall(r'data-(\w+)="([^"]*)"', tag))
        rows[attrs["knob"]] = attrs
    return rows


def test_knobs_lists_every_knob_and_every_weight_entry() -> None:
    html = _client(_config()).get("/knobs").text
    rows = _rows(html)
    assert {k.query_key for k in KNOBS} <= set(rows)
    assert "heat.channel-weights.sre-internal" in rows
    assert "heat.channel-weights.big" in rows
    assert "heat.people-weights.U0AAA" in rows
    # One line per row, so `rg -c 'data-knob="heat\.'` counts rows: the Phase 3 criterion.
    assert len(re.findall(r'data-knob="heat\.', html)) == len(KNOBS) + 3
    assert len(html.splitlines()) > len(KNOBS)
    assert sum('data-knob="heat.' in line for line in html.splitlines()) == len(KNOBS) + 3


def test_each_row_carries_the_yml_value() -> None:
    rows = _rows(_client(_config()).get("/knobs").text)
    assert rows["heat.velocity-weight"]["yml"] == "30"
    assert rows["heat.followed-drop"]["yml"] == "0.2"
    assert rows["heat.atrophy-curve"]["yml"] == "hyperbolic"
    assert rows["heat.channel-weights.sre-internal"]["yml"] == "2"
    assert rows["heat.people-weights.U0AAA"]["yml"] == "1.5"


def test_yml_value_is_the_tick_and_the_choice_is_preselected() -> None:
    html = _client(_config()).get("/knobs").text
    # velocity-weight spans 0..60, yml 30: the tick sits at 50%.
    assert 'data-tick="30" style="left:50.0%"' in html
    assert '<option value="hyperbolic" selected>' in html
    assert '<option value="exponential" selected>' not in html


def test_a_yml_value_past_the_range_clamps_the_tick_and_widens_the_map_slider() -> None:
    groups = {g.name: g for g in panel_groups(_config())}
    big = next(k for k in groups["channel-weights"].knobs if k.label == "#big")
    assert big.hi == "5"
    assert big.tick == 100.0
    zero = next(k for k in groups["size"].knobs if k.key == "heat.base-cap")
    assert 0.0 <= zero.tick <= 100.0


def test_every_panel_key_is_a_key_parse_overrides_accepts() -> None:
    for group in panel_groups(_config()):
        for knob in group.knobs:
            assert parse_overrides([(knob.key, "1")]) == {knob.key: "1"}


def test_map_groups_use_the_map_names_parse_overrides_knows() -> None:
    names = {g.name for g in panel_groups(_config())}
    assert set(MAPS) <= names


def test_topic_group_is_dimmed_when_no_rater_is_configured() -> None:
    html = _client(_config(api_key="")).get("/knobs").text
    assert 'data-dimmed="no rater configured"' in html
    assert "knob-group dim" in html


def test_topic_group_is_live_when_a_rater_is_configured() -> None:
    html = _client(_config(api_key="k")).get("/knobs").text
    assert "data-dimmed" not in html
    assert "knob-group dim" not in html


def test_empty_weight_maps_render_no_empty_sections() -> None:
    config = AppConfig(heat=HeatConfig())
    names = [g.name for g in panel_groups(config)]
    assert "channel-weights" not in names
    assert "people-weights" not in names


def test_panel_ignores_request_overrides() -> None:
    client = _client(_config())
    assert client.get("/knobs?heat.velocity-weight=60").text == client.get("/knobs").text


@pytest.mark.parametrize("path", ["/", "/knobs"])
def test_no_em_dash_in_the_rendered_panel(path: str) -> None:
    assert "—" not in _client(_config()).get(path).text


def test_index_has_the_toggle_the_error_box_and_the_form() -> None:
    html = _client(_config()).get("/").text
    assert 'id="knob-toggle"' in html
    assert 'id="knob-error"' in html
    assert 'id="knob-form"' in html
    assert 'hx-get="/knobs"' in html
