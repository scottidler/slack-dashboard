import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import markdown as md
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from slack_dashboard.config import AppConfig, HeatConfig, resolve_channel_weight
from slack_dashboard.connection import ConnectionState
from slack_dashboard.heat import (
    HeatBreakdown,
    heat_breakdown,
    is_heated,
    is_involved,
    is_vip,
    is_zombie,
    replies_in_window,
)
from slack_dashboard.llm.provider import LlmProvider
from slack_dashboard.slack.mrkdwn import strip_mrkdwn
from slack_dashboard.slack.poller import SlackPoller
from slack_dashboard.thread import ThreadEntry

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"

# Glyphs for the emoji state channel (see design doc "Emoji State Channel").
# Render order in the row: involved (leads), unanswered, new, vip, spiking, pepper, zombie.
_ZOMBIE = "\N{ZOMBIE}"
_PEPPER = "\N{HOT PEPPER}"  # 🌶️ heated exchange (contentious back-and-forth or hostile tone)
_VIP = "\N{CROWN}"
_SPIKING = "\N{HIGH VOLTAGE SIGN}"
_NEW = "\N{SPARKLES}"  # ✨ recently entered the dashboard's view
_UNANSWERED = "\N{BLACK QUESTION MARK ORNAMENT}"  # ❓ arithmetic proxy: question, few replies, aged
_INVOLVED = "\N{BUST IN SILHOUETTE}"  # 👤 the current user has personally posted in this thread

# Heat-strip glyphs (see design doc "The strip"). _SPIKING, _INVOLVED, and _VIP above are
# reused here for velocity, damping, and the base chip's crown so the strip and the row
# speak one glyph vocabulary.
_THERMO = "\N{THERMOMETER}"  # 🌡️ overall heat score
_CHANNEL_WEIGHT = "\N{LABEL}"  # 🏷️ channel_weight multiplier
_BASE = "\N{BAR CHART}"  # 📊 message_count/people_count (+👑 when a VIP is present)
_RECENCY = "\N{STOPWATCH}"  # ⏱️ recency decay multiplier

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
    message_count: int
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
    """Build the fixed overall+5 chip strip for the hover popup from one breakdown.

    Order matches the design doc: overall, channel_weight, base, velocity, atrophy,
    damping. Dimmed (no-op-for-the-score) cases: channel_weight == 1.00 (no multiplier
    effect), velocity_weight == 0.0 (the default - velocity contributes nothing to the
    score even though the chip still reports the raw rate), and damping == 1.00 (not
    involved, or the feature is off). Trivial assembly of already-computed fields - no
    logging per the logging rule.
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
            glyph=_RECENCY,
            value=f"{breakdown.atrophy:.2f}",
            tooltip="working-hours atrophy multiplier (exponential decay since last activity)",
        ),
        HeatChip(
            glyph=_INVOLVED,
            value=f"×{breakdown.damping:.2f}",
            tooltip="involvement damping",
            dimmed=breakdown.damping == 1.00,
        ),
    ]


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
    """True when a participant carries an above-default people-weight (a pinned person).

    Delegates to ``heat.is_vip`` so the VIP rule lives in exactly one place.
    """
    return is_vip(thread, config.heat)


def _emojis(
    thread: ThreadEntry,
    config: AppConfig,
    now: float,
    app_start_at: float,
    self_user_id: str | None = None,
) -> str:
    """Build the glyph string for a thread row.

    Render order: involved (leads), unanswered, new, vip, spiking, pepper, zombie.
    Each glyph is a single Unicode character that signals thread state at a glance.

    ``now`` and ``app_start_at`` are passed in (not read from the wall clock here) so the
    caller can capture them once per request and all rows use a consistent timestamp.
    ``self_user_id`` is the authenticated user's Slack id; None when unresolved, in
    which case the 👤 involved glyph never fires.
    """
    riw = replies_in_window(thread, config.heat)
    new_window = config.heat.new_window_minutes * 60
    min_age = config.heat.unanswered_min_age_hours * 3600
    thread_age = now - float(thread.thread_ts)
    logger.debug(
        "_emojis: channel=%s thread_ts=%s replies_in_window=%d tier=%s"
        " first_observed_at=%.3f now=%.3f app_start_at=%.3f new_window=%ds"
        " unanswered_enabled=%s message_count=%d thread_age_s=%.0f min_age_s=%.0f",
        thread.channel_name,
        thread.thread_ts,
        riw,
        thread.heat_tier,
        thread.first_observed_at,
        now,
        app_start_at,
        new_window,
        config.heat.unanswered_enabled,
        thread.message_count,
        thread_age,
        min_age,
    )
    glyphs = []
    # 👤 involved: the current user has personally posted in this thread (leads the row as
    # the primary triage cue - "am I already in this one?"). participants is keyed by stable
    # Slack user_id and includes every message author, so membership is a plain lookup. An
    # unresolved self_user_id (None) never matches, so the glyph stays dark.
    if is_involved(thread, self_user_id):
        glyphs.append(_INVOLVED)
    # ❓ unanswered: arithmetic proxy for a dropped-ball question (opt-in; off by default).
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
        glyphs.append(_UNANSWERED)
    # ✨ new: recently entered the dashboard's view (not a zombie; not within the
    # app-start suppressor window that masks the first-run / degraded-mode storm).
    if (
        thread.first_observed_at > 0
        and now - thread.first_observed_at < new_window
        and not is_zombie(thread, config.heat)
        and now - app_start_at >= new_window
    ):
        glyphs.append(_NEW)
    if _has_vip(thread, config):
        glyphs.append(_VIP)
    if riw >= config.heat.spiking_threshold:
        glyphs.append(_SPIKING)
    if is_heated(thread, config.heat, now):
        glyphs.append(_PEPPER)
    if is_zombie(thread, config.heat):
        glyphs.append(_ZOMBIE)
    return "".join(glyphs)


def _build_row(
    thread: ThreadEntry,
    config: AppConfig,
    now: float,
    app_start_at: float,
    self_user_id: str | None = None,
    below_fold: bool = False,
) -> RowView:
    return RowView(
        channel_id=thread.channel_id,
        channel_name=thread.channel_name,
        thread_ts=thread.thread_ts,
        display_title=thread.display_title,
        message_count=thread.message_count,
        participant_count=len(thread.participants),
        heat_tier=thread.heat_tier,
        emojis=_emojis(thread, config, now, app_start_at, self_user_id),
        deep_link=deep_link(
            config.workspace, thread.channel_id, thread.thread_ts, config.slack.team_id
        ),
        channel_link=channel_link(config.workspace, thread.channel_id, config.slack.team_id),
        summary=thread.summary,
        below_fold=below_fold,
    )


def group_threads(
    threads: list[ThreadEntry],
    group_by: str,
    config: AppConfig,
    now: float,
    app_start_at: float,
    self_user_id: str | None = None,
) -> list[GroupView]:
    """Partition globally-ranked threads into display groups.

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
                    _build_row(t, config, now, app_start_at, self_user_id, below_fold=_below(i))
                    for i, t in enumerate(threads)
                ],
            )
        ]

    if group_by == "channel":
        groups: dict[str, GroupView] = {}
        for index, thread in enumerate(threads):
            group = groups.get(thread.channel_name)
            if group is None:
                group = GroupView(label=thread.channel_name)
                groups[thread.channel_name] = group
            group.rows.append(
                _build_row(
                    thread, config, now, app_start_at, self_user_id, below_fold=_below(index)
                )
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
            label = _tier_label(thread.message_count, _SIZE_TIERS)
            buckets[label].rows.append(
                _build_row(
                    thread, config, now, app_start_at, self_user_id, below_fold=_below(index)
                )
            )
    else:  # velocity
        buckets = {label: GroupView(label=label) for label, _ in _VELOCITY_TIERS}
        for index, thread in enumerate(threads):
            label = _tier_label(replies_in_window(thread, config.heat), _VELOCITY_TIERS)
            buckets[label].rows.append(
                _build_row(
                    thread, config, now, app_start_at, self_user_id, below_fold=_below(index)
                )
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

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html")

    @app.get("/threads", response_class=HTMLResponse)
    async def threads(
        request: Request,
        group_by: str = Query("none", alias="group-by"),
        compact: bool = Query(True),
    ) -> HTMLResponse:
        # Capture now and app_start_at once per request so every row uses a
        # consistent timestamp (avoids per-row clock drift across many threads).
        now = datetime.now(UTC).timestamp()
        app_start_at = poller.app_start_at
        ranked = poller.ranked_threads()
        groups = group_threads(ranked, group_by, config, now, app_start_at, poller.self_user_id)
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
        # The heat-strip data depends only on entry, not on the summary, so it is built
        # once here and carried by every thread-bearing response below (cached, fresh
        # success, and fresh LLM-failure alike) - only the missing-thread branch above
        # omits it.
        now = datetime.now(UTC).timestamp()
        breakdown = heat_breakdown(entry, config.heat, poller.self_user_id, now)
        heat_chips = _heat_strip(breakdown, config.heat, entry.channel_name)
        # The detail header quotes the thread's first message (its real "title") and
        # attributes it to the author; bullets summarizing the thread render below.
        detail = {
            "quote": strip_mrkdwn(entry.first_message),
            "author": entry.started_by,
            "heat": heat_chips,
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
    async def channel(request: Request, channel_id: str) -> HTMLResponse:
        # Hover popover: every thread in this channel, in the same heat ranking as the
        # main list (ranked_threads is already sorted; we just filter to this channel).
        # Capture now/app_start_at once so all rows use a consistent timestamp.
        now = datetime.now(UTC).timestamp()
        app_start_at = poller.app_start_at
        ranked = poller.ranked_threads()
        rows = [
            _build_row(t, config, now, app_start_at, poller.self_user_id)
            for t in ranked
            if t.channel_id == channel_id
        ]
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
