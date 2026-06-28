import logging
from dataclasses import dataclass, field
from pathlib import Path

import markdown as md
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from slack_dashboard.config import AppConfig, resolve_channel_weight, resolve_person_weight
from slack_dashboard.connection import ConnectionState
from slack_dashboard.heat import is_zombie, replies_in_window
from slack_dashboard.llm.provider import LlmProvider
from slack_dashboard.slack.mrkdwn import strip_mrkdwn
from slack_dashboard.slack.poller import SlackPoller
from slack_dashboard.thread import ThreadEntry

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"

# Glyphs for the emoji state channel (see design doc "Emoji State Channel").
_ZOMBIE = "\N{ZOMBIE}"
_FIRE = "\N{FIRE}"
_VIP = "\N{CROWN}"

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
class RowView:
    channel_id: str
    channel_name: str
    thread_ts: str
    display_title: str
    reply_count: int
    participant_count: int
    heat_tier: str
    emojis: str
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


def _markdown_filter(text: str) -> Markup:
    return Markup(md.markdown(text))


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
    """True when a participant carries an above-default people-weight (a pinned person)."""
    default = float(config.heat.participant_weight)
    return any(resolve_person_weight(uid, config.heat) > default for uid in thread.participants)


def _emojis(thread: ThreadEntry, config: AppConfig) -> str:
    glyphs = []
    if is_zombie(thread, config.heat):
        glyphs.append(_ZOMBIE)
    if _has_vip(thread, config):
        glyphs.append(_VIP)
    if thread.heat_tier == "hot":
        glyphs.append(_FIRE)
    return "".join(glyphs)


def _build_row(thread: ThreadEntry, config: AppConfig, below_fold: bool = False) -> RowView:
    return RowView(
        channel_id=thread.channel_id,
        channel_name=thread.channel_name,
        thread_ts=thread.thread_ts,
        display_title=thread.display_title,
        reply_count=thread.reply_count,
        participant_count=len(thread.participants),
        heat_tier=thread.heat_tier,
        emojis=_emojis(thread, config),
        deep_link=deep_link(
            config.workspace, thread.channel_id, thread.thread_ts, config.slack.team_id
        ),
        channel_link=channel_link(config.workspace, thread.channel_id, config.slack.team_id),
        summary=thread.summary,
        below_fold=below_fold,
    )


def group_threads(threads: list[ThreadEntry], group_by: str, config: AppConfig) -> list[GroupView]:
    """Partition globally-ranked threads into display groups.

    group-by=none is a single label-less group in pure heat order (no headers).
    group-by=channel produces one group per channel, clustered by family (the first
    hyphen-token, so all sre-* / data-platform-* sit together), families ordered by
    their strongest channel's weight. size/velocity bucket threads into
    fixed tier ranges, ordered high-to-low, dropping empty tiers. Threads arrive
    heat-ranked, so rows stay in heat order within every group. Every trackable
    thread is always rendered - there is no cap (see Zero-miss invariant).
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
                rows=[_build_row(t, config, below_fold=_below(i)) for i, t in enumerate(threads)],
            )
        ]

    if group_by == "channel":
        groups: dict[str, GroupView] = {}
        for index, thread in enumerate(threads):
            group = groups.get(thread.channel_name)
            if group is None:
                group = GroupView(label=thread.channel_name)
                groups[thread.channel_name] = group
            group.rows.append(_build_row(thread, config, below_fold=_below(index)))

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
            label = _tier_label(thread.reply_count, _SIZE_TIERS)
            buckets[label].rows.append(_build_row(thread, config, below_fold=_below(index)))
    else:  # velocity
        buckets = {label: GroupView(label=label) for label, _ in _VELOCITY_TIERS}
        for index, thread in enumerate(threads):
            label = _tier_label(replies_in_window(thread, config.heat), _VELOCITY_TIERS)
            buckets[label].rows.append(_build_row(thread, config, below_fold=_below(index)))
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

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html")

    @app.get("/threads", response_class=HTMLResponse)
    async def threads(
        request: Request,
        group_by: str = Query("none", alias="group-by"),
        compact: bool = Query(True),
    ) -> HTMLResponse:
        ranked = poller.ranked_threads()
        groups = group_threads(ranked, group_by, config)
        # Disclosure contract: every ranked thread is always server-rendered (groups carry
        # the full set); compact mode only hides the below-fold tail via CSS. The visible
        # hidden count tells the user how much sits past the fold.
        limit = config.display.compact_rows
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
            },
        )

    @app.get("/summarize/{channel_id}/{thread_ts:path}", response_class=HTMLResponse)
    async def summarize(request: Request, channel_id: str, thread_ts: str) -> HTMLResponse:
        key = (channel_id, thread_ts)
        entry = poller.threads.get(key)
        if entry is None:
            return templates.TemplateResponse(
                request,
                "partials/summary.html",
                {"error": True, "channel_id": channel_id, "thread_ts": thread_ts},
            )
        # The detail header quotes the thread's first message (its real "title") and
        # attributes it to the author; bullets summarizing the thread render below.
        detail = {"quote": strip_mrkdwn(entry.first_message), "author": entry.started_by}
        if entry.summary and entry.summary_watermark >= entry.reply_count:
            return templates.TemplateResponse(
                request, "partials/summary.html", {"summary": entry.summary, **detail}
            )
        messages = [strip_mrkdwn(entry.first_message)]
        summary = await llm.generate_summary(messages)
        if summary is None:
            return templates.TemplateResponse(
                request,
                "partials/summary.html",
                {"error": True, "channel_id": channel_id, "thread_ts": thread_ts},
            )
        entry.summary = summary
        entry.summary_watermark = entry.reply_count
        return templates.TemplateResponse(
            request, "partials/summary.html", {"summary": summary, **detail}
        )

    @app.get("/channel/{channel_id}", response_class=HTMLResponse)
    async def channel(request: Request, channel_id: str) -> HTMLResponse:
        # Hover popover: every thread in this channel, in the same heat ranking as the
        # main list (ranked_threads is already sorted; we just filter to this channel).
        ranked = poller.ranked_threads()
        rows = [_build_row(t, config) for t in ranked if t.channel_id == channel_id]
        name = next((t.channel_name for t in ranked if t.channel_id == channel_id), channel_id)
        link = channel_link(config.workspace, channel_id, config.slack.team_id)
        return templates.TemplateResponse(
            request,
            "partials/channel.html",
            {"rows": rows, "channel_name": name, "channel_link": link},
        )

    @app.post("/dismiss/{channel_id}/{thread_ts:path}", response_class=HTMLResponse)
    async def dismiss(request: Request, channel_id: str, thread_ts: str) -> HTMLResponse:
        poller.dismiss_thread(channel_id, thread_ts)
        # Empty body: HTMX swaps the row out (hx-swap="outerHTML").
        return HTMLResponse("")

    @app.get("/status", response_class=HTMLResponse)
    async def status(request: Request) -> HTMLResponse:
        # "connected" suppresses the banner; absent connection state = assume connected.
        state = connection.status() if connection is not None else "connected"
        return templates.TemplateResponse(request, "partials/status.html", {"status": state})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}
