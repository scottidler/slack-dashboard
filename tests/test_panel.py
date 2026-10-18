"""The knob panel: GET /knobs renders every KNOBS row and weight-map entry with the yml's value."""

import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import slack_dashboard.web as web_mod
from slack_dashboard.config import AppConfig, HeatConfig, TopicsConfig, load_config
from slack_dashboard.knobs import KNOBS, MAPS, apply_overrides, panel_groups, parse_overrides
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


_NAMES = {"U0AAA": "Ada Lovelace"}


def _client(config: AppConfig) -> TestClient:
    app = FastAPI()
    poller = MagicMock()
    poller.resolve_user = AsyncMock(side_effect=lambda uid: _NAMES.get(uid, uid))
    web_mod.create_routes(app, poller, MagicMock(spec=LlmProvider), config)
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


def test_people_rows_show_the_name_on_their_own_scale() -> None:
    html = _client(_config()).get("/knobs").text
    row = re.search(
        r'<div class="knob" data-knob="heat\.people-weights\.U0AAA".*?</div>\s*</div>', html, re.S
    )
    assert row is not None
    assert ">Ada Lovelace</label>" in row.group(0)
    assert 'title="U0AAA: people-weights entry' in row.group(0)
    # People weights are volume points (participant-weight's unit), not channel multipliers.
    assert 'max="20" step="1"' in row.group(0)
    channel = re.search(
        r'<div class="knob" data-knob="heat\.channel-weights\.sre-internal".*?</div>\s*</div>',
        html,
        re.S,
    )
    assert channel is not None
    assert ">#sre-internal</label>" in channel.group(0)
    assert 'max="3" step="0.1"' in channel.group(0)


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


def test_every_slider_end_is_a_value_the_server_accepts() -> None:
    config = _config()
    config.heat.max_thread_age_days = 3  # narrows-only: the slider must stop at the yml
    for group in panel_groups(config):
        for knob in group.knobs:
            if knob.choices:
                continue
            for end in (knob.lo, knob.hi):
                apply_overrides(config, {knob.key: end})  # a ValueError is the 422
    age = next(k for g in panel_groups(config) for k in g.knobs if "max-thread-age" in k.key)
    assert age.hi == "3"
    assert age.tick == 100.0


INDEX = Path(__file__).resolve().parent.parent / "src" / "slack_dashboard" / "templates"


def _render_yml(overrides: dict[str, str]) -> str:
    """The page's own renderYml(), run under node over ``overrides`` as the knob map."""
    node = shutil.which("node")
    assert node, "node is required to run the page's renderYml"
    page = (INDEX / "index.html").read_text()
    match = re.search(r"^( *)function renderYml\(\) \{\n.*?^\1\}$", page, re.M | re.S)
    assert match, "renderYml() not found in index.html"
    script = f"var map = {json.dumps(overrides)};\n{match.group(0)}\n"
    script += "process.stdout.write(renderYml());"
    return subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout


def test_copy_as_yml_loads_back_with_yaml_special_map_names(tmp_path: Path) -> None:
    names = ["on", "123", "null", "yes", "~", "true", "sre-internal"]
    overrides = {f"heat.channel-weights.{n}": "2" for n in names}
    overrides["heat.people-weights.U0AAA"] = "1.5"
    overrides["heat.velocity-weight"] = "40"
    path = tmp_path / "slack-dashboard.yml"
    path.write_text(_render_yml(overrides))
    heat = load_config(path).heat
    assert heat.channel_weights == dict.fromkeys(names, 2.0)
    assert heat.people_weights == {"U0AAA": 1.5}
    assert heat.velocity_weight == 40
