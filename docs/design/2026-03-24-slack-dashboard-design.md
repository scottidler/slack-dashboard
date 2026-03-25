# Design Document: Slack Dashboard

**Author:** Scott Idler
**Date:** 2026-03-24 (revised 2026-03-25)
**Status:** In Review
**Review Passes Completed:** 3/3

## Summary

A situational awareness dashboard for a Director of Platform overseeing SRE and Data Platform teams. Monitors 22 Slack channels via Socket Mode (real-time WebSocket events) with REST API backfill, ranks threads by a decay-weighted heat algorithm, and auto-generates LLM titles and summaries. The goal is a single-screen, glanceable view of where the fires are - conversations flare up and die out visually.

## Problem Statement

### Background

As Director of Platform at Tatari, Scott oversees SRE and Data Platform teams with a combined footprint of 22 channels (internal, shared, and public) across the Slack workspace (~7,400 total channels). Important conversations - incident threads, architectural decisions, cross-team coordination - happen throughout the day and are impossible to track manually.

### Problem

There is no single view of "what's happening right now" across all these channels. Staying informed requires manually checking each channel, scanning for active threads, and reading enough context to understand what matters. A 100+ reply thread in one channel can go unnoticed while checking another.

### Goals

- Single-screen situational awareness: see conversations flare up and die out
- Active threads from today rise to the top; old threads from last week/month sink away
- Auto-generated titles and summaries so you don't have to read every thread
- Real-time updates via Socket Mode; no wasted polling when nothing is happening
- All ranking knobs exposed in XDG config for tuning

### Non-Goals

- Bot functionality (no posting, reacting, responding)
- Persistent storage or database
- Authentication on the dashboard
- Multi-user support
- DMs or group DMs
- Heat map / grid UI (future enhancement - current MVP uses ranked card list)

## Proposed Solution

### Overview

A single Docker container running a Python/FastAPI app with four concerns:

1. **Socket Mode Listener** - WebSocket connection to Slack for real-time message events
2. **REST Fetcher** - Priority-queue-driven fetcher for initial backfill and thread enrichment
3. **Thread Ranker** - Decay-weighted heat algorithm that naturally pushes today's active threads to the top
4. **Web Server** - FastAPI + HTMX + Pico CSS dashboard with markdown-rendered summaries

### Architecture

```
Slack WebSocket  -->  Socket Mode Listener  -->  Priority Queue
                                                      |
Browser (HTMX)  <-->  FastAPI Web Server         REST Fetcher  -->  Thread Store
                          |                           |
                    LLM API (titles/summaries)   Slack REST API
```

**Data flow:**

1. On startup, seed the priority queue with all 22 channels for initial history fetch (low priority).
2. Socket Mode connects immediately and receives message events in real-time.
3. When a Socket Mode event arrives for a thread: update metadata directly from the event (reply count, last activity, participant), queue high-priority REST fetch for full thread replies.
4. REST fetcher pulls from priority queue. High-priority (Socket Mode triggered) before low-priority (initial backfill).
5. When queue is empty, fetcher sleeps on queue.get() - no polling, no wasted API calls.
6. Periodic low-priority refresh every N minutes to catch reconnection gaps.
7. LLM auto-generates titles and summaries for all threads as they're fetched.

### Data Model

**Thread entry** (in-memory dict keyed by `(channel_id, thread_ts)`):

| Field | Type | Description |
|-------|------|-------------|
| channel_id | str | Slack channel ID (part of composite key) |
| channel_name | str | Human-readable channel name |
| thread_ts | str | Slack thread timestamp (part of composite key) |
| first_message | str | Text of the root message |
| author_id | str | User ID of thread starter |
| author_name | str | Display name of thread starter (resolved via users:read) |
| reply_count | int | Total replies in thread |
| participants | dict[str, int] | User ID -> message count in thread |
| last_activity | datetime | Timestamp of most recent reply |
| heat_score | float | Computed heat value (decay-weighted) |
| heat_tier | str | "hot", "warm", or "cold" |
| title | str or None | LLM-generated 5-8 word title |
| title_watermark | int | Reply count when title was last generated |
| summary | str or None | LLM-generated summary (auto-generated) |
| summary_watermark | int | Reply count when summary was last generated |

**Priority queue item:**

| Field | Type | Description |
|-------|------|-------------|
| priority | int | 0 = Socket Mode triggered, 10 = initial backfill, 20 = periodic refresh |
| channel_id | str | Slack channel ID |
| channel_name | str | Human-readable name |
| thread_ts | str or None | Specific thread (None = fetch channel history) |

### Configuration

`~/.config/slack-dashboard/slack-dashboard.yml`:

```yaml
slack:
  token: "${SLACK_USER_TOKEN}"
  app-token: "${SLACK_APP_TOKEN}"

channels:
  ai-foundry: C0ACWPXHLPK
  ai-technical: C04T9RZANF6
  ask-security: C2BDSAAQ7
  backstage: C09PCEA4T8F
  cloud-costs: C058YU74X1G
  data-platform: C01T2NKEWJ0
  data-platform-internal: C023YTN4B1D
  eng-on-call: C01FDD3NYDY
  engineering: C0L0DJU56
  engineering-mgmt: GMW66K3NC
  incidents: C01A1FH5SAT
  it-helpdesk: C02U9V18Q6T
  opex-monthly: C06SR71MLNV
  platform-internal: C039YLDJW5T
  scrum-of-scrums: C068HD6TSNX
  sre: C01FXAT07G9
  sre-internal: C01FXF7P3ST
  sre-it: C0A0P2N7R7D
  sre-sec: C02SL43UW9J
  sre-solutions: C05JAH4PEMA
  staging-env: C024BGMSXL7
  tech-spec-reviews: C06CB6EU3BY

heat:
  reply-weight: 2
  participant-weight: 3
  hot-threshold: 50
  warm-threshold: 20
  retitle-reply-growth: 5
  retitle-reply-percent: 25
  decay-hours: 24
  max-thread-age-days: 3

fetch:
  min-replies: 3
  refresh-interval-minutes: 10

llm:
  provider: anthropic
  model: claude-haiku-4-5-20251001
  api-key: "${ANTHROPIC_API_KEY}"

server:
  host: "0.0.0.0"
  port: 8889
  log-level: info
```

Key design decisions:
- Channels are a `name: id` dict, not a name list. Eliminates the 37-page `conversations.list` crawl on startup (workspace has ~7,400 channels).
- All heat algorithm knobs exposed in config with sane defaults.
- Secrets via `${VAR_NAME}` interpolation from environment variables.
- Kebab-case keys mapped to snake_case via Pydantic alias support.

### Thread Heat Algorithm

**Decay-weighted formula:**

```
base = (reply_count * reply-weight) + (participant_count * participant-weight)
decay = max(0.01, 1.0 - (hours_since_last_activity / decay-hours))
heat = base * decay
```

With `decay-hours: 24`:
- Thread active 1 hour ago: ~96% of base score
- Thread active 12 hours ago: ~50% of base score
- Thread active 23 hours ago: ~4% of base score
- Thread active 24+ hours ago: 1% floor (effectively invisible but not pruned)
- Thread active > `max-thread-age-days` ago: excluded entirely

This means today's 10-reply thread always outranks last week's 200-reply thread.

**Staleness filter:** Threads where `last_activity` is older than `max-thread-age-days` are excluded from the dashboard entirely.

**Tier classification** (determines visual indicator, not polling - Socket Mode replaces polling):
- **Hot** (heat >= `hot-threshold`) - red dot
- **Warm** (heat >= `warm-threshold`) - orange dot
- **Cold** (heat < `warm-threshold`) - blue dot

### Socket Mode Integration

Using `slack_sdk.socket_mode.aiohttp.SocketModeClient`:

- Connects via WebSocket using app-level token (`xapp-...` with `connections:write`)
- Subscribes to `message.channels` and `message.groups` events
- Filters to configured channel IDs only
- For thread replies (`thread_ts` != `ts`): update thread metadata, queue high-priority REST fetch
- For new thread parents: track if they accumulate replies above `min-replies`
- Must acknowledge every event via `SocketModeResponse(envelope_id=...)`
- Auto-reconnect enabled by default in SDK (ping-pong heartbeat, stale detection)
- Shares same `AsyncWebClient` instance as REST fetcher

### REST API Integration

Using `slack_sdk.web.async_client.AsyncWebClient` with user token:

- **Rate limiting:** SDK-native `AsyncRateLimitErrorRetryHandler` (3 retries) + 1.2s pacing between calls via semaphore
- **Channel history:** `conversations.history` to find threads with >= `min-replies`
- **Thread replies:** `conversations.replies` for full thread data and participant lists
- **Priority queue:** `asyncio.PriorityQueue` with deduplication set. Socket Mode events jump the queue.
- **Quiescent when idle:** `queue.get()` blocks when empty. No polling loop. No wasted calls at night.

### LLM Integration

Using `anthropic` Python SDK async client (Claude Haiku):

**Auto-titling:** Generate 5-8 word title from thread reply texts. Fire-and-forget after REST fetch. Cache with reply-count watermark. Regenerate when replies grow beyond threshold.

**Auto-summarization:** Generate summary with one-sentence overview + bulleted key points (markdown rendered in browser). Fire-and-forget for all threads. Cache with watermark. Regenerate when new replies arrive.

**Prompts:** Hardened to prevent refusals. System prompt explicitly states input is raw Slack messages, instructs to never ask for more information, never refuse.

**Message preprocessing:** Slack mrkdwn stripped before LLM: user mentions -> @user, channel links -> #channel, emoji codes removed, bold/italic/code stripped.

**Provider abstraction:** Simple interface for swapping providers later.

### API Design

| Method | Path | Purpose | Response |
|--------|------|---------|----------|
| GET | `/` | Main dashboard page | Full HTML page |
| GET | `/threads` | Thread list update | HTMX partial |
| GET | `/summarize/{channel_id}/{thread_ts}` | Generate/return summary | HTMX partial |
| GET | `/health` | Health check | 200 OK |

### Web Frontend

**Thread card display:**
- Channel name (color-coded badge)
- Thread author name (who started it)
- Auto-generated title (LLM) or truncated first message (fallback)
- Reply count, last activity timestamp
- Heat indicator (colored dot: red/orange/blue)
- Participants: collapsed by default showing "N participants", expandable to a bulleted list of display names sorted by message count (highest first), with count shown left of each name
- Auto-generated summary (markdown rendered with bullets)
- "Summarize" button only as fallback if auto-summary hasn't populated yet
- Deep link to open thread in Slack

**HTMX polling:**
- `hx-trigger="every 60s [document.visibilityState === 'visible']"` (longer interval since Socket Mode provides real-time)
- Visibility-change listener for immediate refresh on tab re-focus
- Page goes inert when tab loses focus

**Styling:** Pico CSS - classless, dark mode, zero custom CSS.

**Future:** Redesign from scrolling card list to dense heat map / grid visual. Single-screen, glanceable. Conversations flare up and die out visually.

### Deployment

**Dockerfile:** Python 3.12 slim, `uv`, `uvicorn`.

**Runtime:**
```bash
docker run -d \
  -e SLACK_USER_TOKEN=xoxp-... \
  -e SLACK_APP_TOKEN=xapp-... \
  -e ANTHROPIC_API_KEY=sk-... \
  -e XDG_CONFIG_HOME=/config \
  -v ~/.config/slack-dashboard:/config/slack-dashboard \
  -p 8889:8889 \
  slack-dashboard
```

**Local dev:** `.env` file with `python-dotenv`, `uv run slack-dashboard`.

**k8s readiness:** Stateless, single container. Config via ConfigMap + Secrets for EKS promotion.

### Slack App Setup

1. Create Slack App at api.slack.com
2. OAuth & Permissions: add `channels:read`, `channels:history`, `groups:read`, `groups:history` as User Token Scopes
3. Enable Socket Mode: toggle ON
4. App-Level Tokens: generate token with `connections:write` scope -> `SLACK_APP_TOKEN`
5. Event Subscriptions: subscribe to `message.channels` and `message.groups`
6. Install to workspace, copy User OAuth Token -> `SLACK_USER_TOKEN`

## Alternatives Considered

### Alternative 1: Rust + Leptos (full-stack Rust)

- **Description:** Leptos for SSR + client-side hydration, all Rust.
- **Pros:** No JS/TS. Single language. Great performance. Author knows Rust well.
- **Cons:** Steep Leptos learning curve. Slow compile-time feedback loop. Slack API crates immature.
- **Why not chosen:** Python's slack_sdk and anthropic SDK are first-class. Faster to functional.

### Alternative 2: Rust + Axum + HTMX (hybrid)

- **Description:** Axum backend, Askama templates, HTMX frontend.
- **Pros:** Rust backend, no JS, fast runtime, Axum is mature.
- **Cons:** Slack API crate ecosystem thin. More boilerplate.
- **Why not chosen:** Productivity gain from Python SDKs outweighs Rust runtime advantages.

### Alternative 3: Polling only (no Socket Mode)

- **Description:** Keep the current polling architecture.
- **Pros:** Simpler. No app-level token needed.
- **Cons:** Slow initial load (minutes). Wastes API calls at night. Cannot achieve real-time. Rate limiting is a constant problem.
- **Why not chosen:** Socket Mode eliminates all of these problems with minimal added complexity.

## Technical Considerations

### Dependencies

- **Python packages:** `fastapi`, `uvicorn`, `slack-sdk`, `anthropic`, `jinja2`, `pyyaml`, `pydantic`, `httpx`, `aiohttp`, `python-dotenv`, `markdown`
- **Frontend:** Pico CSS (CDN), HTMX (CDN)
- **External services:** Slack API (user token + app token), Anthropic API

### Performance

- Socket Mode events arrive in <1 second
- REST enrichment adds 1-2 seconds per thread (one API call through semaphore)
- Initial backfill is rate-limited but active threads jump the queue via Socket Mode
- At night: zero API calls, zero CPU, idle WebSocket connection + sleeping queue consumer

### Security

- Slack user token (`xoxp-...`): read-only access to channels. Environment variable only.
- Slack app token (`xapp-...`): `connections:write` only, cannot read messages. Environment variable only.
- Anthropic API key: environment variable only.
- Dashboard has no authentication - LAN access only.
- Jinja2 auto-escapes by default. Markdown rendered via `markdown` library (summary content is LLM-generated, not user-injected).

### Testing Strategy

- **Unit tests:** Heat algorithm (decay, staleness filter), config parsing, thread data model, mrkdwn preprocessing, priority queue ordering
- **Integration tests:** Slack client with mocked responses, LLM client with mocked responses, Socket Mode event handler with mocked client
- **Manual testing:** Real Slack workspace, post messages, verify real-time updates

### Rollout Plan

1. Fix heat algorithm (decay multiplier, staleness filter) - immediate
2. Socket Mode implementation (listener + priority queue fetcher) - next session
3. UI redesign (heat map / dense grid) - future
4. Docker container on desk.lan or EKS - when stable

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Socket Mode disconnects during events | Low | Low | SDK auto-reconnects; periodic refresh catches gaps |
| App-level token revoked | Low | High | Health check monitors; fall back to periodic REST refresh |
| LLM produces garbage summaries | Medium | Low | Hardened prompts; plain-text fallback; watermark invalidation |
| Heat algorithm ranks poorly | Medium | Low | All knobs in config; iterate on real data |
| Rate limiting during initial backfill | Medium | Low | SDK retry handler; 1.2s pacing; Socket Mode events jump queue |
| Stale threads dominate dashboard | High | Medium | Decay multiplier + max-thread-age-days filter |

## Open Questions

- [ ] Best default for max-thread-age-days: 3? 7? Configurable (yes, already planned).
- [ ] Server-Sent Events (SSE) for instant frontend push, or HTMX polling at 60s sufficient?
- [ ] Content-aware heat scoring via LLM (detect urgency, spicy language) - future enhancement?
- [x] Resolve Slack user IDs to display names - needed for participant list. Add `users:read` scope. Cache user ID -> name mapping.
- [ ] Heat map / grid UI design - what does the dense visual look like?

## References

- [Slack Socket Mode](https://docs.slack.dev/apis/events-api/using-socket-mode/)
- [slack_sdk Socket Mode client](https://docs.slack.dev/tools/python-slack-sdk/socket-mode/)
- [Slack API - conversations.history](https://api.slack.com/methods/conversations.history)
- [Slack API - conversations.replies](https://api.slack.com/methods/conversations.replies)
- [HTMX documentation](https://htmx.org/docs/)
- [Pico CSS](https://picocss.com/)
- [FastAPI documentation](https://fastapi.tiangolo.com/)
