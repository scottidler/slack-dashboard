from pathlib import Path

import markdown as md
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from slack_dashboard.llm.provider import LlmProvider
from slack_dashboard.slack.mrkdwn import strip_mrkdwn
from slack_dashboard.slack.poller import SlackPoller

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _markdown_filter(text: str) -> Markup:
    return Markup(md.markdown(text))


def create_routes(
    app: FastAPI,
    poller: SlackPoller,
    llm: LlmProvider,
    templates: Jinja2Templates | None = None,
) -> None:
    if templates is None:
        templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
    templates.env.filters["markdown"] = _markdown_filter

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html")

    @app.get("/threads", response_class=HTMLResponse)
    async def threads(request: Request) -> HTMLResponse:
        ranked = poller.ranked_threads()
        return templates.TemplateResponse(request, "partials/threads.html", {"threads": ranked})

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

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}
