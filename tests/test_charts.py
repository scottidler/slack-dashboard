"""The shaping chart code exists once: src/slack_dashboard/static/chart.js, loaded by both pages."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import slack_dashboard.web as web_mod
from slack_dashboard.config import AppConfig
from slack_dashboard.llm.provider import LlmProvider

ROOT = Path(__file__).resolve().parent.parent
CHART_JS = ROOT / "src" / "slack_dashboard" / "static" / "chart.js"
EXPLORE_HTML = ROOT / "bin" / "explore.html"
APP_PAGE = ROOT / "src" / "slack_dashboard" / "templates" / "index.html"


def _explore() -> object:
    spec = importlib.util.spec_from_file_location("explore", ROOT / "bin" / "explore.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_both_pages_load_the_same_chart_file() -> None:
    assert "static/chart.js" in APP_PAGE.read_text()
    assert "static/chart.js" in EXPLORE_HTML.read_text()
    assert CHART_JS.is_file()


def test_chart_is_defined_in_exactly_one_file() -> None:
    sources = [CHART_JS, EXPLORE_HTML, APP_PAGE]
    defining = [p for p in sources if "function chart(" in p.read_text()]
    assert defining == [CHART_JS]


def test_app_serves_chart_js() -> None:
    app = FastAPI()
    web_mod.create_routes(app, MagicMock(), MagicMock(spec=LlmProvider), AppConfig())
    resp = TestClient(app).get("/static/chart.js")
    assert resp.status_code == 200
    assert "function chart(" in resp.text


def test_published_explorer_inlines_chart_js() -> None:
    page = _explore().render_page("{}")  # type: ignore[attr-defined]
    assert "function chart(" in page
    assert "<script src=" not in page  # the tag was replaced, not left dangling
    assert "const D = {};" in page


def test_explorer_build_fails_loudly_when_it_no_longer_loads_the_chart_file(
    tmp_path: Path,
) -> None:
    module = _explore()
    module._TEMPLATE = tmp_path / "explore.html"  # type: ignore[attr-defined]
    module._TEMPLATE.write_text("<script>const D = __DATA__;</script>")  # type: ignore[attr-defined]
    try:
        module.render_page("{}")  # type: ignore[attr-defined]
    except ValueError as exc:
        assert "chart.js" in str(exc)
    else:
        raise AssertionError("render_page accepted a template without the chart tag")
