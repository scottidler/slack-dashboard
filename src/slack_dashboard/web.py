import logging
from dataclasses import dataclass, field
from pathlib import Path

import markdown as md
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from slack_dashboard.config import AppConfig
from slack_dashboard.connection import ConnectionState
from slack_dashboard.heat import is_zombie, velocity
from slack_dashboard.llm.provider import LlmProvider
from slack_dashboard.slack.mrkdwn import strip_mrkdwn
from slack_dashboard.slack.poller import SlackPoller
from slack_dashboard.thread import ThreadEntry

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"

# Glyphs for the emoji state channel (see design doc "Emoji State Channel").
_ZOMBIE = "\N{ZOMBIE}"
_FIRE = "\N{FIRE}"

_GROUP_BY_CHOICES = ("channel", "size", "velocity", "participants")


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
    summary: str | None


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


def _emojis(thread: ThreadEntry, config: AppConfig) -> str:
    glyphs = []
    if is_zombie(thread, config.heat):
        glyphs.append(_ZOMBIE)
    if thread.heat_tier == "hot":
        glyphs.append(_FIRE)
    return "".join(glyphs)


def _build_row(thread: ThreadEntry, config: AppConfig) -> RowView:
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
        summary=thread.summary,
    )


def group_threads(threads: list[ThreadEntry], group_by: str, config: AppConfig) -> list[GroupView]:
    """Partition globally-ranked threads into display groups.

    group-by=channel produces one group per channel (groups ordered by their
    hottest thread). The size/velocity/participants modes are a single ordered
    group sorted by that dimension (v1: sort, not bucket). Every trackable thread
    is always rendered - there is no cap (see Zero-miss invariant).
    """
    logger.debug("group_threads: group_by=%s count=%d", group_by, len(threads))
    if group_by not in _GROUP_BY_CHOICES:
        group_by = "channel"

    if group_by == "channel":
        groups: dict[str, GroupView] = {}
        for thread in threads:
            group = groups.get(thread.channel_name)
            if group is None:
                group = GroupView(label=thread.channel_name)
                groups[thread.channel_name] = group
            group.rows.append(_build_row(thread, config))
        # Threads arrive heat-ranked, so the first row in each group is its hottest;
        # order groups by that hottest row's position (preserved by dict insertion).
        return list(groups.values())

    key_fns = {
        "size": lambda t: t.reply_count,
        "velocity": lambda t: velocity(t, config.heat),
        "participants": lambda t: len(t.participants),
    }
    ordered = sorted(threads, key=key_fns[group_by], reverse=True)
    return [GroupView(label=group_by, rows=[_build_row(t, config) for t in ordered])]


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
        group_by: str = Query("channel", alias="group-by"),
    ) -> HTMLResponse:
        ranked = poller.ranked_threads()
        groups = group_threads(ranked, group_by, config)
        return templates.TemplateResponse(
            request,
            "partials/threads.html",
            {"groups": groups, "group_by": group_by},
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
        if entry.summary and entry.summary_watermark >= entry.reply_count:
            return templates.TemplateResponse(
                request, "partials/summary.html", {"summary": entry.summary}
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
        return templates.TemplateResponse(request, "partials/summary.html", {"summary": summary})

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
