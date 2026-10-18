"""Request overrides: heat.* query parameters re-rank the live board for one request only.

Every route test runs a real ``SlackPoller`` over a fixed board with time frozen at
``FROZEN`` (``datetime`` is replaced in heat, poller and web), so a score is a fixed number
and two requests compare byte for byte.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import slack_dashboard.heat as heat_mod
import slack_dashboard.slack.poller as poller_mod
import slack_dashboard.web as web_mod
from slack_dashboard.config import AppConfig, HeatConfig
from slack_dashboard.dismiss import DismissStore
from slack_dashboard.events import EventStore
from slack_dashboard.knobs import (
    KNOBS,
    NOT_A_VIEW_KNOB,
    apply_overrides,
    parse_overrides,
)
from slack_dashboard.llm.provider import LlmProvider, SummaryResult
from slack_dashboard.slack.client import SlackClient
from slack_dashboard.slack.poller import SlackPoller
from slack_dashboard.thread import ReplyRecord, ThreadEntry

# Wednesday 08:00 in the default work window (America/Los_Angeles).
FROZEN = datetime(2026, 10, 14, 15, 0, 0, tzinfo=UTC)
T = FROZEN.timestamp()


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> "_FrozenDatetime":
        frozen = FROZEN if tz is not None else FROZEN.replace(tzinfo=None)
        return frozen  # type: ignore[return-value]


class _NoLlm(LlmProvider):
    async def generate_title(self, messages: list[str]) -> str | None:
        raise AssertionError("no LLM call expected")

    async def generate_summary(self, messages: list[str]) -> SummaryResult:
        raise AssertionError("no LLM call expected")


YML = AppConfig(
    workspace="tatari",
    heat=HeatConfig(
        followed_drop=0.2,
        followed_rebuild_per_msg=0.0,
        channel_weights={"sre-internal": 1.5},
        tier_hot_count=1,
        tier_warm_count=3,
    ),
)


def _thread(
    channel_id: str,
    channel_name: str,
    root_ago_min: float,
    reply_ago_min: list[float],
    people: list[str],
    title: str,
    **kw: Any,
) -> ThreadEntry:
    root_ts = T - root_ago_min * 60
    replies = [ReplyRecord(root_ts, people[0], title, True)]
    for i, ago in enumerate(reply_ago_min):
        replies.append(ReplyRecord(T - ago * 60, people[(i + 1) % len(people)], f"r{i}", False))
    participants: dict[str, int] = {}
    for r in replies:
        participants[r.author_id] = participants.get(r.author_id, 0) + 1
    return ThreadEntry(
        channel_id=channel_id,
        channel_name=channel_name,
        thread_ts=f"{root_ts:.6f}",
        first_message=title,
        started_by=people[0],
        message_count=len(replies),
        participants=participants,
        last_activity=datetime.fromtimestamp(max(r.ts for r in replies), UTC),
        replies=replies,
        first_seen_ts=root_ts,
        summary=f"- summary of {title}",
        summary_watermark=len(replies),
        **kw,
    )


def _board() -> list[ThreadEntry]:
    six = ["U1", "U2", "U3", "U4", "U5", "U6"]
    return [
        # big: lots of volume, last post an hour ago, nothing in the velocity window
        _thread(
            "C1", "sre-internal", 26 * 60, [70 + i for i in range(29)], six, "big outage review"
        ),
        # burst: small, every reply inside the last 15 minutes
        _thread("C2", "data-eng", 20, [14, 11, 8, 5, 2], ["U7", "U8", "U9"], "burst in data eng"),
        # followed: mid-size, Slack follows it for me
        _thread(
            "C1",
            "sre-internal",
            180,
            [150, 120, 90, 60, 45, 40, 35, 33, 32, 31, 31.5],
            ["U1", "U2", "U3"],
            "followed question",
            subscribed=True,
        ),
        _thread("C3", "general", 300, [280, 260, 240], ["U2", "U4"], "small cold chat"),
        _thread("C2", "data-eng", 30, [25, 20, 15, 10], ["U1", "U2"], "dismissed thread"),
        # stale: last post five days ago, past the yml's 3-day age
        _thread(
            "C1",
            "sre-internal",
            7 * 24 * 60,
            [5 * 24 * 60 + i for i in range(5)],
            ["U1", "U2"],
            "stale thread",
        ),
        # quiet two days: inside the yml's 3-day age, outside a 1-day override
        _thread(
            "C3",
            "general",
            3 * 24 * 60,
            [2 * 24 * 60 + i for i in range(3)],
            ["U5", "U6"],
            "two days quiet",
        ),
    ]


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    for mod in (heat_mod, poller_mod, web_mod):
        monkeypatch.setattr(mod, "datetime", _FrozenDatetime)


@pytest.fixture
def poller(tmp_path: Path, frozen: None) -> SlackPoller:
    dismiss = DismissStore(tmp_path / "dismissed.jsonl")
    poller = SlackPoller(
        AsyncMock(spec=SlackClient),
        YML,
        dismiss=dismiss,
        events=EventStore(tmp_path / "events.jsonl"),
    )
    for t in _board():
        poller.threads[(t.channel_id, t.thread_ts)] = t
    dismissed = next(t for t in poller.threads.values() if t.first_message == "dismissed thread")
    dismiss.dismiss(dismissed.channel_id, dismissed.thread_ts)
    return poller


@pytest.fixture
def client(poller: SlackPoller) -> TestClient:
    app = FastAPI()
    web_mod.create_routes(app, poller, _NoLlm(), YML)
    return TestClient(app)


def _thread_by_title(poller: SlackPoller, title: str) -> ThreadEntry:
    return next(t for t in poller.threads.values() if t.first_message == title)


_ROW = re.compile(r'<span class="row-title heat-(\w+)">([^<]+)</span>')


def _rows(html: str) -> list[tuple[str, str]]:
    """(title, tier) per rendered row, in board order."""
    return [(title, tier) for tier, title in _ROW.findall(html)]


def _events(poller: SlackPoller) -> list[dict[str, Any]]:
    assert poller._events is not None
    return [json.loads(line) for line in poller._events.path.read_text().splitlines()]


# ---- the table ----


def test_every_heat_field_is_a_knob_or_named_not_a_view_knob() -> None:
    fields = set(HeatConfig.model_fields)
    knobs = {k.key for k in KNOBS}
    assert knobs.isdisjoint(NOT_A_VIEW_KNOB)
    assert knobs | set(NOT_A_VIEW_KNOB) == fields  # a stale name on either side fails
    assert len(knobs) == len(KNOBS)


@pytest.mark.parametrize("knob", KNOBS, ids=lambda k: k.key)
def test_every_knob_range_end_is_a_value_heat_config_accepts(knob: Any) -> None:
    values = list(knob.choices) if knob.choices else [knob.lo, knob.hi]
    for value in values:
        HeatConfig.model_validate({knob.key: value})


# ---- parse and apply ----


def test_parse_overrides_keeps_heat_keys_last_value_wins() -> None:
    params = [
        ("group-by", "channel"),
        ("heat.velocity-weight", "1"),
        ("heat.channel-weights.sre-internal", "2"),
        ("heat.velocity-weight", "60"),
    ]
    assert parse_overrides(params) == {
        "heat.velocity-weight": "60",
        "heat.channel-weights.sre-internal": "2",
    }


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("heat.bogus", "unknown override heat.bogus"),
        ("heat.velocity_weight", "unknown override heat.velocity_weight"),
        ("heat.decay-hours", "unknown override heat.decay-hours"),
        ("heat.channel-weights", "names no entry"),
        ("heat.channel-weights.", "names no entry"),
    ],
)
def test_parse_overrides_rejects_unknown_keys(key: str, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        parse_overrides([(key, "1")])


def test_apply_overrides_without_overrides_is_the_yml_config() -> None:
    assert apply_overrides(YML, {}) is YML


def test_channel_weights_override_adds_and_replaces_without_touching_the_yml() -> None:
    cfg = apply_overrides(
        YML,
        {"heat.channel-weights.sre-internal": "2", "heat.channel-weights.new-chan": "0.5"},
    )
    assert cfg.heat.channel_weights == {"sre-internal": 2.0, "new-chan": 0.5}
    assert YML.heat.channel_weights == {"sre-internal": 1.5}


def test_people_weights_override_adds_an_id() -> None:
    cfg = apply_overrides(YML, {"heat.people-weights.U7": "9"})
    assert cfg.heat.people_weights == {"U7": 9.0}


def test_scalar_override_is_coerced_and_validated() -> None:
    cfg = apply_overrides(YML, {"heat.velocity-weight": "30", "heat.atrophy-curve": "hyperbolic"})
    assert cfg.heat.velocity_weight == 30.0
    assert cfg.heat.atrophy_curve == "hyperbolic"
    assert YML.heat.velocity_weight == 0.0


def test_max_thread_age_days_may_narrow_but_not_widen() -> None:
    assert apply_overrides(YML, {"heat.max-thread-age-days": "1"}).heat.max_thread_age_days == 1
    with pytest.raises(ValueError, match="may not exceed the yml's 3"):
        apply_overrides(YML, {"heat.max-thread-age-days": "4"})


# ---- routes ----


def test_override_never_writes_into_shared_entries(client: TestClient, poller: SlackPoller) -> None:
    yml_order = [(t.first_message, t.heat_tier) for t in poller.ranked_threads()]
    stored = {k: (t.heat_score, t.heat_tier) for k, t in poller.threads.items()}
    assert yml_order[:2] == [("big outage review", "hot"), ("burst in data eng", "warm")]

    response = client.get("/threads?heat.channel-weights.data-eng=3")
    assert response.status_code == 200
    rows = _rows(response.text)
    # The override reorders the board and re-tiers it...
    assert rows[:2] == [("burst in data eng", "hot"), ("big outage review", "warm")]
    # ...and the shared entries and the yml ranking are exactly as they were.
    assert {k: (t.heat_score, t.heat_tier) for k, t in poller.threads.items()} == stored
    assert [(t.first_message, t.heat_tier) for t in poller.ranked_threads()] == yml_order


def test_request_without_overrides_returns_the_yml_order_and_tiers(
    client: TestClient, poller: SlackPoller
) -> None:
    client.get("/threads?heat.channel-weights.data-eng=3&heat.tier-hot-count=3")
    rows = _rows(client.get("/threads").text)
    assert rows == [(t.first_message, t.heat_tier) for t in poller.ranked_threads()]


def test_dismissed_and_stale_threads_stay_out_under_overrides(client: TestClient) -> None:
    titles = [title for title, _ in _rows(client.get("/threads?heat.velocity-weight=60").text)]
    assert "dismissed thread" not in titles
    assert "stale thread" not in titles
    assert "two days quiet" in titles
    narrowed = client.get("/threads?heat.max-thread-age-days=1").text
    assert "two days quiet" not in [title for title, _ in _rows(narrowed)]


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("heat.bogus=1", "unknown override heat.bogus"),
        ("heat.velocity-weight=abc", "heat.velocity-weight: Input should be a valid number"),
        ("heat.velocity-weight=nan", "heat.velocity-weight: Input should be a finite number"),
        ("heat.channel-weights.x=inf", "heat.channel-weights.x: Input should be a finite number"),
        ("heat.reply-weight=-1", "heat reply-weight (-1) must be >= 0"),
        ("heat.base-cap=0", "heat base-cap (0.0) must be greater than 0"),
        ("heat.max-thread-age-days=4", "may not exceed the yml's 3"),
    ],
)
@pytest.mark.parametrize("path", ["/threads", "/summarize/C1/{ts}", "/channel/C1"])
def test_rejected_override_is_a_422_with_the_reason(
    client: TestClient, poller: SlackPoller, path: str, query: str, message: str
) -> None:
    ts = _thread_by_title(poller, "big outage review").thread_ts
    response = client.get(f"{path.format(ts=ts)}?{query}")
    assert response.status_code == 422
    assert message in response.text
    assert response.headers["content-type"].startswith("text/plain")


def test_summarize_with_an_override_prints_the_overridden_score(
    client: TestClient, poller: SlackPoller
) -> None:
    burst = _thread_by_title(poller, "burst in data eng")
    url = f"/summarize/{burst.channel_id}/{burst.thread_ts}"
    thermo = re.compile(r'title="heat score">\N{THERMOMETER}(\d+)<')
    plain = thermo.search(client.get(url).text)
    knobbed = thermo.search(client.get(url + "?heat.channel-weights.data-eng=3").text)
    assert plain is not None and knobbed is not None
    assert (plain.group(1), knobbed.group(1)) == ("29", "87")


def test_velocity_override_changes_the_strip_on_a_thread_with_replies_in_the_window(
    client: TestClient, poller: SlackPoller
) -> None:
    # The Phase 2 success criterion's shape: heat.velocity-weight=60 on a thread with replies
    # inside the velocity window renders a different strip, not only a different root attr.
    burst = _thread_by_title(poller, "burst in data eng")
    url = f"/summarize/{burst.channel_id}/{burst.thread_ts}"

    def strip(html: str) -> str:
        return html.split('<div class="heat-strip">')[1].split("</div>")[0]

    plain = client.get(url).text
    knobbed = client.get(url + "?heat.velocity-weight=60").text
    assert strip(plain) != strip(knobbed)
    assert 'title="heat score">\N{THERMOMETER}29<' in plain
    assert 'title="heat score">\N{THERMOMETER}41<' in knobbed


def test_channel_popover_ranks_under_the_override(client: TestClient) -> None:
    plain = client.get("/channel/C2").text
    knobbed = client.get("/channel/C2?heat.channel-weights.data-eng=3").text
    assert 'chan-title heat-warm">burst in data eng' in plain
    assert 'chan-title heat-hot">burst in data eng' in knobbed


# Renders of this board taken from the code before overrides existed (commit 4a5fb49), with
# the same frozen clock. No overrides must stay byte-identical to them; a deliberate template
# change re-baselines these by re-rendering the old and new code side by side.
_BASELINE_SHA256 = {
    "/threads": "95a888f5cb8115ffe92414d3661a9f25e3a8cc7953be6a23b675f7ea42a0e928",
    "/threads?group-by=channel&compact=false": (
        "143de2bb147aece6f8599f5b56a11a8a8691ac075ed3a635b573d0ca0853b9d9"
    ),
    "/summarize/{followed}": "aad5824cc4fa4c36d325ed68d2dbf921834228b08b321a0cba0ea6c0d018092a",
    "/channel/C1": "53c4f74be8be0008ab410253d5f8a74b4bf4d1b7280d5ac6ddf60586fc518481",
}


@pytest.mark.parametrize("url", list(_BASELINE_SHA256))
def test_no_overrides_is_byte_identical_to_the_pre_override_render(
    client: TestClient, poller: SlackPoller, url: str
) -> None:
    followed = _thread_by_title(poller, "followed question")
    path = url.format(followed=f"{followed.channel_id}/{followed.thread_ts}")
    response = client.get(path)
    assert response.status_code == 200
    assert "data-overrides" not in response.text
    assert hashlib.sha256(response.content).hexdigest() == _BASELINE_SHA256[url]


@pytest.mark.parametrize("path", ["/threads", "/summarize/C1/{ts}", "/channel/C1"])
def test_partial_root_carries_the_overrides_it_was_rendered_with(
    client: TestClient, poller: SlackPoller, path: str
) -> None:
    ts = _thread_by_title(poller, "big outage review").thread_ts
    html = client.get(f"{path.format(ts=ts)}?heat.velocity-weight=30&group-by=none").text
    attr = re.search(r'data-overrides="([^"]*)"', html)
    assert attr is not None
    assert json.loads(attr.group(1).replace("&#34;", '"')) == {"heat.velocity-weight": "30"}


def test_open_and_dismiss_with_overrides_carry_them_on_the_event(
    client: TestClient, poller: SlackPoller
) -> None:
    burst = _thread_by_title(poller, "burst in data eng")
    big = _thread_by_title(poller, "big outage review")
    query = "?heat.channel-weights.data-eng=3"
    assert client.post(f"/event/open/{burst.channel_id}/{burst.thread_ts}{query}").status_code
    assert client.post(f"/dismiss/{big.channel_id}/{big.thread_ts}{query}").status_code == 200
    opened, dismissed = _events(poller)
    assert opened["kind"] == "open"
    assert opened["overrides"] == {"heat.channel-weights.data-eng": "3"}
    # The rank is the yml's: the event records where the yml board held the thread.
    assert opened["rank"] == 2
    assert dismissed["kind"] == "dismiss"
    assert dismissed["overrides"] == {"heat.channel-weights.data-eng": "3"}


def test_open_and_dismiss_without_overrides_have_no_overrides_key(
    client: TestClient, poller: SlackPoller
) -> None:
    burst = _thread_by_title(poller, "burst in data eng")
    big = _thread_by_title(poller, "big outage review")
    assert client.post(f"/event/open/{burst.channel_id}/{burst.thread_ts}").status_code == 204
    assert client.post(f"/dismiss/{big.channel_id}/{big.thread_ts}").status_code == 200
    events = _events(poller)
    assert [e["kind"] for e in events] == ["open", "dismiss"]
    assert all("overrides" not in e for e in events)


def test_poller_eligible_drops_dismissed_and_stale_and_writes_nothing(
    poller: SlackPoller,
) -> None:
    before = {k: (t.heat_score, t.heat_tier) for k, t in poller.threads.items()}
    titles = {t.first_message for t in poller.eligible(YML.heat, T)}
    assert titles == {
        "big outage review",
        "burst in data eng",
        "followed question",
        "small cold chat",
        "two days quiet",
    }
    narrowed = YML.heat.model_copy(update={"max_thread_age_days": 1})
    assert "two days quiet" not in {t.first_message for t in poller.eligible(narrowed, T)}
    assert {k: (t.heat_score, t.heat_tier) for k, t in poller.threads.items()} == before


# ---- shaping charts ----


def test_knobs_inputs_is_one_row_per_board_thread_with_the_overridden_score(
    client: TestClient,
) -> None:
    rows = client.get("/knobs/inputs").json()
    board = _rows(client.get("/threads").text)
    assert len(rows) == len(board) == 5  # dismissed, stale and (yml 3-day) none else
    assert set(rows[0]) == {"volume", "since-last", "p-leader", "score"}
    overridden = client.get("/knobs/inputs?heat.velocity-weight=60").json()
    assert len(overridden) == len(rows)
    assert sorted(r["score"] for r in overridden) != sorted(r["score"] for r in rows)
    assert max(r["since-last"] for r in rows) >= 0
    assert all(r["p-leader"] is None for r in rows)  # no jev rating on the fixture board


def test_knobs_inputs_volume_follows_the_reply_weight_override(client: TestClient) -> None:
    base = sorted(r["volume"] for r in client.get("/knobs/inputs").json())
    heavy = sorted(r["volume"] for r in client.get("/knobs/inputs?heat.reply-weight=3").json())
    assert heavy != base
    assert all(h >= b for h, b in zip(heavy, base, strict=True))


def test_knobs_inputs_narrowed_age_drops_rows_like_threads(client: TestClient) -> None:
    query = "?heat.max-thread-age-days=1"
    assert len(client.get("/knobs/inputs" + query).json()) == len(
        _rows(client.get("/threads" + query).text)
    )


def test_knobs_inputs_rejects_a_bad_override_with_422(client: TestClient) -> None:
    resp = client.get("/knobs/inputs?heat.velocity-weight=abc")
    assert resp.status_code == 422
    assert "heat.velocity-weight" in resp.text
