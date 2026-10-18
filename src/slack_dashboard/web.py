import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import markdown as md
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from slack_dashboard.config import (
    MARK_STATES,
    AppConfig,
    HeatConfig,
    WorkWindowConfig,
    resolve_channel_weight,
)
from slack_dashboard.connection import ConnectionState
from slack_dashboard.heat import (
    HeatBreakdown,
    Ranked,
    heat_breakdown,
    is_following,
    is_heated,
    is_vip,
    is_zombie,
    replies_in_window,
    score_threads,
)
from slack_dashboard.knobs import (
    OverrideError,
    RequestConfig,
    panel_groups,
    request_config,
    shaping_inputs,
)
from slack_dashboard.llm.provider import LlmProvider
from slack_dashboard.slack.mrkdwn import strip_mrkdwn
from slack_dashboard.slack.poller import SlackPoller
from slack_dashboard.thread import ThreadEntry
from slack_dashboard.worktime import business_hours_between

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

# Row flags: the thread states that used to be an emoji column, now plain words with a
# tooltip each. Render order: following (leads), unanswered, new, vip, spiking, heated, revived.
_FLAG_HELP: dict[str, str] = {
    "following": "Slack follows this thread for you (started, commented, mentioned, or followed)",
    "unanswered": "a question with few replies that has aged past the floor (opt-in proxy)",
    "new": "entered the dashboard's view within the last hour",
    "vip": "a weighted person is in the thread",
    "spiking": "15+ replies in the velocity window",
    "heated": "a contentious back-and-forth or a hostile tone",
    "revived": "an old thread that went quiet and has new activity",
}
# Row marks: one fixed slot per state, always rendered, dimmed when off, so the same glyph
# sits at the same x down the whole board. Order: the jev topics (p >= _TAG_THRESHOLD, the
# explorer's cut; non-work when p(work) < _NON_WORK_THRESHOLD), then the state flags.
_TAG_THRESHOLD = 0.6
_NON_WORK_THRESHOLD = 0.4
_TOPIC_HELP: dict[str, str] = {
    "leader": "jev: needs a manager or director to decide, approve, assign or escalate",
    "access": "jev: a request for access, permissions, roles or credentials",
    "incident": "jev: an outage, breakage or degradation being handled",
    "resolved": "jev: already answered, fixed or closed",
    "social": "jev: social or personal, or not about work",
}
_MARK_HELP: dict[str, str] = {**_TOPIC_HELP, **_FLAG_HELP}

# Activity strip: one cell per working hour over the last _STRIP_HOURS, newest at the
# right; the cell's height is the number of posts in that hour (1, 2-3, 4-7, 8+). Drawn as
# CSS boxes, not font glyphs, so the column has the same edges on every viewer.
_STRIP_HOURS = 12

# The strip still uses these three glyphs for velocity, damping and the base chip's crown.
_VIP = "\N{CROWN}"
_SPIKING = "\N{HIGH VOLTAGE SIGN}"
_FOLLOWING_DAMPING = "\N{BUST IN SILHOUETTE}"

# Heat-strip glyphs (see design doc "The strip").
_THERMO = "\N{THERMOMETER}"  # 🌡️ overall heat score
_CHANNEL_WEIGHT = "\N{LABEL}"  # 🏷️ channel_weight multiplier
_BASE = "\N{BAR CHART}"  # 📊 message_count/people_count (+👑 when a VIP is present)
_RECENCY = "\N{STOPWATCH}"  # ⏱️ atrophy decay multiplier
# Heat-remodel chips (see design doc "Data Model" / worktime): time_alive and
# time_since_last, both fractional working hours. Distinct glyphs from _RECENCY (the
# atrophy multiplier itself) so the strip can show the raw working-hours inputs alongside
# the decay they drive.
_TIME_ALIVE = "\N{HOURGLASS}"  # ⌛ time_alive: working hours, first-post -> last-post
_TIME_SINCE_LAST = "\N{TIMER CLOCK}"  # ⏲ time_since_last: working hours, last-post -> now
# Topic chips (jev probabilities, see topics.py): p(needs a leader) feeds the leader vote,
# gate and half-life stretch; p(resolved) feeds the resolved gate. Both show "?" until rated.
_LEADER = "\N{DIRECT HIT}"  # 🎯 p(needs a leader)
_RESOLVED = "\N{WHITE HEAVY CHECK MARK}"  # ✅ p(resolved)

_GROUP_BY_CHOICES = ("none", "channel", "size", "velocity")

# Bucket tiers for the size/velocity grouping modes. Each is (label, inclusive lower
# bound), ordered high-to-low; the first tier a value clears wins. The last tier is the
# catch-all floor. Labels carry their range so the group header is self-explanatory.
_SIZE_TIERS = (
    ("huge (100+)", 100),
    ("large (50-99)", 50),
    ("medium (25-49)", 25),
    ("small (3-24)", 0),
)
_VELOCITY_TIERS = (
    ("spiking (15+)", 15),
    ("active (1-14)", 1),
    ("idle (0)", 0),
)


def _tier_label(value: int, tiers: tuple[tuple[str, int], ...]) -> str:
    for label, lower in tiers:
        if value >= lower:
            return label
    return tiers[-1][0]


@dataclass
class RowMark:
    """One slot in a row's mark column: the glyph, whether it is on, and its tooltip."""

    key: str
    glyph: str
    on: bool
    help: str


@dataclass
class RowView:
    channel_id: str
    channel_name: str
    thread_ts: str
    display_title: str
    message_count: int
    participant_count: int
    heat_tier: str
    # The explorer's row: an activity strip (one level per working hour, 12 hours, newest
    # right; empty hours are None), working hours since the last post, then the marks: one
    # fixed slot per state, lit or dim. The score itself stays on the hover strip.
    strip: list[int | None]
    since_last: float
    marks: list[RowMark]
    deep_link: str
    channel_link: str
    summary: str | None
    # True when this thread's global heat rank is past the compact fold. The row is still
    # rendered into the DOM (zero-miss); compact mode hides it via CSS.
    below_fold: bool = False


@dataclass
class GroupView:
    label: str
    rows: list[RowView] = field(default_factory=list)


@dataclass
class HeatChip:
    """One rendered chip in the hover popup's heat-metrics strip.

    Pure presentation: glyph + already-formatted face value + native tooltip text, plus
    whether this chip is a no-op for the score and should render dimmed. The template only
    iterates; all formatting/dimming logic lives here so there is one place to read it.
    """

    glyph: str
    value: str
    tooltip: str
    dimmed: bool = False


def _heat_strip(breakdown: HeatBreakdown, config: HeatConfig, channel_name: str) -> list[HeatChip]:
    """Build the fixed overall+7 chip strip for the hover popup from one breakdown.

    Order matches the design doc: overall, channel_weight, base, velocity, time_alive,
    time_since_last, atrophy, damping, then the two topic chips (leader, resolved). The two
    working-hours chips sit beside the atrophy chip they feed (time_since_last IS the
    atrophy input; time_alive feeds alive_boost).
    Dimmed (no-op-for-the-score) cases: channel_weight == 1.00 (no multiplier effect),
    velocity_weight == 0.0 (the default - velocity contributes nothing to the score even
    though the chip still reports the raw rate), time_alive dimmed when alive_weight ==
    0.0 (the default - display-only until calibration says otherwise, per the design doc),
    damping == 1.00 (not followed, or the feature is off), and a topic chip when the thread
    is unrated or no knob reads that probability. Trivial assembly of already-computed
    fields - no logging per the logging rule.
    """
    base_value = f"{breakdown.message_count}m·{breakdown.people_count}p"
    if breakdown.has_vip:
        base_value += _VIP
    return [
        HeatChip(
            glyph=_THERMO,
            value=f"{breakdown.overall:.0f}",
            tooltip="heat score",
        ),
        HeatChip(
            glyph=_CHANNEL_WEIGHT,
            value=f"×{breakdown.channel_weight:.2f}",
            tooltip=f"#{channel_name} channel weight",
            dimmed=breakdown.channel_weight == 1.00,
        ),
        HeatChip(
            glyph=_BASE,
            value=base_value,
            tooltip=f"base {breakdown.base:.1f} · people {breakdown.people_term:.1f}",
        ),
        HeatChip(
            glyph=_SPIKING,
            value=f"{breakdown.velocity:.1f}",
            tooltip="replies/min in window (contribution = vel × velocity_weight)",
            dimmed=config.velocity_weight == 0.0,
        ),
        HeatChip(
            glyph=_TIME_ALIVE,
            value=f"{breakdown.time_alive:.1f}h",
            tooltip="time alive: working hours, first post -> last post (feeds alive_boost)",
            dimmed=config.alive_weight == 0.0,
        ),
        HeatChip(
            glyph=_TIME_SINCE_LAST,
            value=f"{breakdown.time_since_last:.1f}h",
            tooltip="time since last: working hours, last post -> now (the atrophy input)",
        ),
        HeatChip(
            glyph=_RECENCY,
            value=f"{breakdown.atrophy:.2f}",
            tooltip="working-hours atrophy multiplier (exponential decay since last activity)",
        ),
        HeatChip(
            glyph=_FOLLOWING_DAMPING,
            value=f"×{breakdown.damping:.2f}",
            tooltip="following damping",
            dimmed=breakdown.damping == 1.00,
        ),
        HeatChip(
            glyph=_LEADER,
            value="?" if breakdown.p_leader is None else f"{breakdown.p_leader:.2f}",
            tooltip=(
                "jev p(needs a leader): feeds the leader vote, the leader gate and the "
                f"half-life stretch (votes +{breakdown.votes:.1f})"
            ),
            dimmed=breakdown.p_leader is None
            or (
                config.leader_weight == 0.0
                and config.leader_gate_floor == 1.0
                and config.atrophy_leader_stretch == 0.0
            ),
        ),
        HeatChip(
            glyph=_RESOLVED,
            value="?" if breakdown.p_resolved is None else f"{breakdown.p_resolved:.2f}",
            tooltip=f"jev p(resolved): feeds the resolved gate (gates ×{breakdown.gates:.2f})",
            dimmed=breakdown.p_resolved is None or config.resolved_drop == 0.0,
        ),
    ]


def _markdown_filter(text: str) -> Markup:
    return Markup(md.markdown(text))


def _strip_level(count: int) -> int:
    """Block height for a working hour's post count: 1 -> 1, 2-3 -> 2, 4-7 -> 3, 8+ -> 4."""
    if count <= 0:
        return 0
    if count == 1:
        return 1
    if count <= 3:
        return 2
    if count <= 7:
        return 3
    return 4


def _activity_strip(thread: ThreadEntry, work: WorkWindowConfig, now: float) -> list[int | None]:
    """Posts per working hour over the last ``_STRIP_HOURS``, oldest first, as block levels.

    Each post (the root and every retained reply) lands in the bucket for how many working
    hours back it was; nights and weekends are skipped, so a strip always spans twelve
    working hours however many calendar days that is. An hour with no posts is ``None``
    (the dim floor in the template). Posts more than twelve working hours back fall off the
    left edge, which is the point: the strip is the thread's recent shape.
    """
    buckets = [0] * _STRIP_HOURS
    stamps = {f"{ts:.6f}" for ts in thread.reply_timestamps}
    stamps.add(f"{float(thread.thread_ts):.6f}")
    for key in stamps:
        back = business_hours_between(float(key), now, work)
        if back < _STRIP_HOURS:
            buckets[_STRIP_HOURS - 1 - int(back)] += 1
    logger.debug(
        "_activity_strip: channel=%s thread_ts=%s posts=%d buckets=%s",
        thread.channel_id,
        thread.thread_ts,
        len(stamps),
        buckets,
    )
    return [_strip_level(n) if n else None for n in buckets]


def _topic_tags(thread: ThreadEntry) -> list[str]:
    """The jev topics a row marks: every topic at p >= 0.6; social also when p(work) < 0.4.

    An unrated thread marks nothing; the marks appear once the rater has run.
    """
    topics = thread.topics
    if topics is None:
        return []
    tags = [
        q
        for q in ("leader", "access", "incident", "resolved", "social")
        if topics.get(q, 0.0) >= _TAG_THRESHOLD
    ]
    if "social" not in tags and "work" in topics and topics["work"] < _NON_WORK_THRESHOLD:
        tags.append("social")
    return tags


def _marks(on: set[str], glyphs: dict[str, str]) -> list[RowMark]:
    """Every slot in MARK_STATES order with its configured glyph, lit when its key is in ``on``."""
    return [RowMark(key, glyphs[key], key in on, _MARK_HELP[key]) for key in MARK_STATES]


def deep_link(workspace: str, channel_id: str, thread_ts: str, team_id: str = "") -> str:
    """Slack deep link for a thread.

    With a team id configured, emit the `slack://` URL scheme so the click hands
    off to the native desktop (Electron) app instead of opening a browser tab. The
    `message` param carries the dotted ts so the app lands on the exact thread.

    Without a team id, fall back to a web link: the fast archives URL when a workspace
    subdomain is set, else the workspace-agnostic `app_redirect` form (prior behavior)
    rather than emit a broken `https://.slack.com/...` link.
    """
    if team_id:
        return f"slack://channel?team={team_id}&id={channel_id}&message={thread_ts}"
    if not workspace:
        return f"https://slack.com/app_redirect?channel={channel_id}&message_ts={thread_ts}"
    ts = thread_ts.replace(".", "")
    return f"https://{workspace}.slack.com/archives/{channel_id}/p{ts}"


def channel_link(workspace: str, channel_id: str, team_id: str = "") -> str:
    """Deep link to a channel's root (no specific message). Same desktop-vs-web rules
    as `deep_link`: slack:// when a team id is set, else a web fallback."""
    if team_id:
        return f"slack://channel?team={team_id}&id={channel_id}"
    if not workspace:
        return f"https://slack.com/app_redirect?channel={channel_id}"
    return f"https://{workspace}.slack.com/archives/{channel_id}"


def _has_vip(thread: ThreadEntry, config: AppConfig) -> bool:
    """True when a participant carries an above-default people-weight (a pinned person).

    Delegates to ``heat.is_vip`` so the VIP rule lives in exactly one place.
    """
    return is_vip(thread, config.heat)


def _flags(
    thread: ThreadEntry,
    config: AppConfig,
    now: float,
    app_start_at: float,
) -> list[str]:
    """The thread's state flags for a row, as words (keys of ``_FLAG_HELP``).

    Render order: following (leads), unanswered, new, vip, spiking, heated, revived.

    ``now`` and ``app_start_at`` are passed in (not read from the wall clock here) so the
    caller can capture them once per request and all rows use a consistent timestamp.
    """
    riw = replies_in_window(thread, config.heat)
    new_window = config.heat.new_window_minutes * 60
    min_age = config.heat.unanswered_min_age_hours * 3600
    thread_age = now - float(thread.thread_ts)
    logger.debug(
        "_flags: channel=%s thread_ts=%s replies_in_window=%d"
        " first_observed_at=%.3f now=%.3f app_start_at=%.3f new_window=%ds"
        " unanswered_enabled=%s message_count=%d thread_age_s=%.0f min_age_s=%.0f",
        thread.channel_name,
        thread.thread_ts,
        riw,
        thread.first_observed_at,
        now,
        app_start_at,
        new_window,
        config.heat.unanswered_enabled,
        thread.message_count,
        thread_age,
        min_age,
    )
    flags: list[str] = []
    # following: Slack follows this thread for me (leads the row as the primary triage
    # cue, "will Slack tell me about this one anyway?"). Read from the root's subscribed.
    if is_following(thread):
        flags.append("following")
    # unanswered: arithmetic proxy for a dropped-ball question (opt-in; off by default).
    # Fires when enabled AND the first message contains "?" AND reply_count is at or below
    # the max-replies floor AND the thread is older than the age floor. The broader
    # contains-? reading is used (vs ends-with-?) to catch questions embedded in longer
    # messages (e.g. "see above, can we fix this?"). Effective primarily in ops channels
    # running channel-min-replies: 1; standard channels require 3+ replies to surface.
    if (
        config.heat.unanswered_enabled
        and "?" in thread.first_message
        and thread.message_count <= config.heat.unanswered_max_replies
        and thread_age >= min_age
    ):
        flags.append("unanswered")
    # new: recently entered the dashboard's view (not revived; not within the app-start
    # suppressor window that masks the first-run / degraded-mode storm).
    if (
        thread.first_observed_at > 0
        and now - thread.first_observed_at < new_window
        and not is_zombie(thread, config.heat)
        and now - app_start_at >= new_window
    ):
        flags.append("new")
    if _has_vip(thread, config):
        flags.append("vip")
    if riw >= config.heat.spiking_threshold:
        flags.append("spiking")
    if is_heated(thread, config.heat, now):
        flags.append("heated")
    if is_zombie(thread, config.heat):
        flags.append("revived")
    return flags


def _build_row(
    ranked: Ranked,
    config: AppConfig,
    now: float,
    app_start_at: float,
    below_fold: bool = False,
) -> RowView:
    # ``ranked`` and ``config`` are the request's: the tier (row colour, strip colour) and the
    # breakdown (the Nh atrophy input) come from the ranking the viewer is looking at, never
    # from the score the poller last wrote into the shared entry.
    thread = ranked.entry
    breakdown = heat_breakdown(thread, config.heat, now)
    return RowView(
        channel_id=thread.channel_id,
        channel_name=thread.channel_name,
        thread_ts=thread.thread_ts,
        display_title=thread.display_title,
        message_count=thread.message_count,
        participant_count=len(thread.participants),
        heat_tier=ranked.tier,
        strip=_activity_strip(thread, config.heat.work_window, now),
        since_last=breakdown.time_since_last,
        marks=_marks(
            set(_topic_tags(thread)) | set(_flags(thread, config, now, app_start_at)),
            config.display.mark_glyphs(),
        ),
        deep_link=deep_link(
            config.workspace, thread.channel_id, thread.thread_ts, config.slack.team_id
        ),
        channel_link=channel_link(config.workspace, thread.channel_id, config.slack.team_id),
        summary=thread.summary,
        below_fold=below_fold,
    )


def group_threads(
    threads: list[Ranked],
    group_by: str,
    config: AppConfig,
    now: float,
    app_start_at: float,
) -> list[GroupView]:
    """Partition globally-ranked threads into display groups.

    ``threads`` is a ranking from ``heat.score_threads`` under the request's ``config``.
    group-by=none is a single label-less group in pure heat order (no headers).
    group-by=channel produces one group per channel, clustered by family (the first
    hyphen-token, so all sre-* / data-platform-* sit together), families ordered by
    their strongest channel's weight. size/velocity bucket threads into
    fixed tier ranges, ordered high-to-low, dropping empty tiers. Threads arrive
    heat-ranked, so rows stay in heat order within every group. Every trackable
    thread is always rendered - there is no cap (see Zero-miss invariant).

    ``now`` and ``app_start_at`` are passed in so every row uses a consistent
    timestamp (captured once per request, not per-row).
    """
    logger.debug("group_threads: group_by=%s count=%d", group_by, len(threads))
    if group_by not in _GROUP_BY_CHOICES:
        group_by = "none"

    # The compact fold is by GLOBAL heat rank: threads arrive heat-ranked, so a thread at
    # index i sits past the fold when i >= compact_rows (0 = no fold). Grouping reorders
    # rows visually but the fold flag stays pinned to the thread's global rank, so compact
    # mode always shows the same globally-hottest subset whatever the grouping.
    limit = config.display.compact_rows

    def _below(index: int) -> bool:
        return limit > 0 and index >= limit

    if group_by == "none":
        return [
            GroupView(
                label="",
                rows=[
                    _build_row(t, config, now, app_start_at, below_fold=_below(i))
                    for i, t in enumerate(threads)
                ],
            )
        ]

    if group_by == "channel":
        groups: dict[str, GroupView] = {}
        for index, thread in enumerate(threads):
            name = thread.entry.channel_name
            group = groups.get(name)
            if group is None:
                group = GroupView(label=name)
                groups[name] = group
            group.rows.append(
                _build_row(thread, config, now, app_start_at, below_fold=_below(index))
            )

        # Cluster channel groups by family (the first hyphen-delimited token, so all
        # `sre-*` sit together and all `data-platform-*` sit together) and order the
        # families by their strongest channel's weight. This keeps related channels
        # adjacent for at-a-glance scanning instead of letting equal-weight channels from
        # different families interleave. Within a family, order by channel weight; the
        # stable sort then preserves hottest-thread order for equal-weight channels.
        def _family(name: str) -> str:
            return name.split("-", 1)[0]

        # groups insertion order is hottest-thread order (threads arrive heat-ranked), so a
        # family's first-appearance index is a heat-driven, non-arbitrary tiebreak - used
        # instead of alphabetical so equal-weight families keep their heat order.
        family_max: dict[str, float] = {}
        family_order: dict[str, int] = {}
        for idx, grp in enumerate(groups.values()):
            fam = _family(grp.label)
            weight = resolve_channel_weight(grp.label, config.heat)
            family_max[fam] = max(family_max.get(fam, 0.0), weight)
            family_order.setdefault(fam, idx)

        return sorted(
            groups.values(),
            key=lambda g: (
                -family_max[_family(g.label)],
                family_order[_family(g.label)],
                -resolve_channel_weight(g.label, config.heat),
            ),
        )

    if group_by == "size":
        buckets = {label: GroupView(label=label) for label, _ in _SIZE_TIERS}
        for index, thread in enumerate(threads):
            label = _tier_label(thread.entry.message_count, _SIZE_TIERS)
            buckets[label].rows.append(
                _build_row(thread, config, now, app_start_at, below_fold=_below(index))
            )
    else:  # velocity
        buckets = {label: GroupView(label=label) for label, _ in _VELOCITY_TIERS}
        for index, thread in enumerate(threads):
            label = _tier_label(replies_in_window(thread.entry, config.heat), _VELOCITY_TIERS)
            buckets[label].rows.append(
                _build_row(thread, config, now, app_start_at, below_fold=_below(index))
            )
    return [group for group in buckets.values() if group.rows]


def create_routes(
    app: FastAPI,
    poller: SlackPoller,
    llm: LlmProvider,
    config: AppConfig,
    connection: ConnectionState | None = None,
    templates: Jinja2Templates | None = None,
) -> None:
    if templates is None:
        templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
    templates.env.filters["markdown"] = _markdown_filter
    # Every read route (and the two event routes) builds its config from the yml plus the
    # request's heat.* overrides; a rejected override is a 422 whose body is the reason.
    view_config = request_config(config)

    async def _override_rejected(request: Request, exc: Exception) -> Response:
        return PlainTextResponse(str(exc), status_code=422)

    app.add_exception_handler(OverrideError, _override_rejected)

    def _rank(heat: HeatConfig, now: float) -> list[Ranked]:
        # Pure: the request's ranking never writes into the shared entries, so a viewer's
        # overrides cannot leak into the event log, the poller, or another viewer.
        return score_threads(poller.eligible(heat, now), heat, now)

    # chart.js is shared with bin/explore.html, which inlines the same file when published.
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html")

    @app.get("/knobs", response_class=HTMLResponse)
    async def knobs_panel(request: Request) -> HTMLResponse:
        # The panel shows the yml's values; the browser layers the viewer's overrides on top.
        return templates.TemplateResponse(
            request, "partials/knobs.html", {"groups": panel_groups(config)}
        )

    @app.get("/knobs/inputs")
    async def knobs_inputs(
        view: Annotated[RequestConfig, Depends(view_config)],
    ) -> JSONResponse:
        # One row per thread /threads would render under the same overrides, at one instant.
        now = datetime.now(UTC).timestamp()
        heat = view.config.heat
        return JSONResponse(shaping_inputs(_rank(heat, now), heat, now))

    @app.get("/threads", response_class=HTMLResponse)
    async def threads(
        request: Request,
        view: Annotated[RequestConfig, Depends(view_config)],
        group_by: str = Query("none", alias="group-by"),
        compact: bool = Query(True),
    ) -> HTMLResponse:
        # Capture now and app_start_at once per request so every row uses a
        # consistent timestamp (avoids per-row clock drift across many threads).
        now = datetime.now(UTC).timestamp()
        app_start_at = poller.app_start_at
        ranked = _rank(view.config.heat, now)
        groups = group_threads(ranked, group_by, view.config, now, app_start_at)
        # Disclosure contract: every ranked thread is always server-rendered (groups carry
        # the full set); compact mode only hides the below-fold tail via CSS. The visible
        # hidden count tells the user how much sits past the fold.
        limit = view.config.display.compact_rows
        total = len(ranked)
        hidden = max(0, total - limit) if limit > 0 else 0
        return templates.TemplateResponse(
            request,
            "partials/threads.html",
            {
                "groups": groups,
                "group_by": group_by,
                "compact": compact,
                "total": total,
                "hidden": hidden,
                "limit": limit,
                "overrides_json": view.overrides_json,
            },
        )

    @app.get("/summarize/{channel_id}/{thread_ts:path}", response_class=HTMLResponse)
    async def summarize(
        request: Request,
        channel_id: str,
        thread_ts: str,
        view: Annotated[RequestConfig, Depends(view_config)],
    ) -> HTMLResponse:
        key = (channel_id, thread_ts)
        entry = poller.threads.get(key)
        if entry is None:
            return templates.TemplateResponse(
                request,
                "partials/summary.html",
                {
                    "error": True,
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                    "overrides_json": view.overrides_json,
                },
            )
        # The heat-strip data depends only on entry and the request's config, not on the
        # summary, so it is built once here and carried by every thread-bearing response
        # below (cached, fresh success, and fresh LLM-failure alike) - only the
        # missing-thread branch above omits it. The strip prints the viewer's ranking.
        now = datetime.now(UTC).timestamp()
        breakdown = heat_breakdown(entry, view.config.heat, now)
        heat_chips = _heat_strip(breakdown, view.config.heat, entry.channel_name)
        # The detail header quotes the thread's first message (its real "title") and
        # attributes it to the author; bullets summarizing the thread render below.
        detail = {
            "quote": strip_mrkdwn(entry.first_message),
            "author": entry.started_by,
            "heat": heat_chips,
            "overrides_json": view.overrides_json,
        }
        if entry.summary and entry.summary_watermark >= entry.message_count:
            return templates.TemplateResponse(
                request, "partials/summary.html", {"summary": entry.summary, **detail}
            )
        # Tone rides this summary call; feed the full retained exchange
        # (entry.replies via summary_texts), not just the root message, so the
        # tone score reflects the whole conversation.
        messages = [strip_mrkdwn(t) for t in entry.summary_texts]
        result = await llm.generate_summary(messages)
        if result.bullets is None:
            return templates.TemplateResponse(
                request,
                "partials/summary.html",
                {"error": True, "channel_id": channel_id, "thread_ts": thread_ts, **detail},
            )
        entry.summary = result.bullets
        entry.summary_watermark = entry.message_count
        entry.heated_tone = result.tone
        return templates.TemplateResponse(
            request, "partials/summary.html", {"summary": result.bullets, **detail}
        )

    @app.get("/channel/{channel_id}", response_class=HTMLResponse)
    async def channel(
        request: Request,
        channel_id: str,
        view: Annotated[RequestConfig, Depends(view_config)],
    ) -> HTMLResponse:
        # Hover popover: every thread in this channel, in the same heat ranking as the
        # main list (the request's ranking, filtered to this channel).
        # Capture now/app_start_at once so all rows use a consistent timestamp.
        now = datetime.now(UTC).timestamp()
        app_start_at = poller.app_start_at
        ranked = [r for r in _rank(view.config.heat, now) if r.entry.channel_id == channel_id]
        rows = [_build_row(r, view.config, now, app_start_at) for r in ranked]
        name = ranked[0].entry.channel_name if ranked else channel_id
        link = channel_link(config.workspace, channel_id, config.slack.team_id)
        return templates.TemplateResponse(
            request,
            "partials/channel.html",
            {
                "rows": rows,
                "channel_name": name,
                "channel_link": link,
                "overrides_json": view.overrides_json,
            },
        )

    @app.post("/dismiss/{channel_id}/{thread_ts:path}", response_class=HTMLResponse)
    async def dismiss(
        request: Request,
        channel_id: str,
        thread_ts: str,
        view: Annotated[RequestConfig, Depends(view_config)],
    ) -> HTMLResponse:
        # The query carries the overrides of the surface the dismiss was clicked on
        # (index.html copies its data-overrides), so the event names that board.
        poller.dismiss_thread(channel_id, thread_ts, view.overrides or None)
        # Empty body: HTMX swaps the row out (hx-swap="outerHTML").
        return HTMLResponse("")

    @app.post("/event/open/{channel_id}/{thread_ts:path}")
    async def open_event(
        channel_id: str,
        thread_ts: str,
        view: Annotated[RequestConfig, Depends(view_config)],
    ) -> Response:
        # Fired by a sendBeacon on the row link's click, so the slack:// navigation is never
        # held up by it. The poller writes the event with the thread's board position, and
        # the clicked surface's overrides (from the beacon's query) when it had any.
        poller.open_thread(channel_id, thread_ts, view.overrides or None)
        return Response(status_code=204)

    @app.get("/status", response_class=HTMLResponse)
    async def status(request: Request) -> HTMLResponse:
        # "connected" suppresses the banner; absent connection state = assume connected.
        state = connection.status() if connection is not None else "connected"
        return templates.TemplateResponse(request, "partials/status.html", {"status": state})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}
