# slack-dashboard

A single, scannable triage pane over Slack channels you can no longer skim. Monitors
channels in real time (Socket Mode + REST hybrid), heat-ranks threads you are **not**
tagged in, and surfaces them as dense, one-line rows.

## Design principle: maximum information density

**This UI exists to pack a great deal of information into a small area.** Every layout
decision optimizes for density and at-a-glance scanning, NOT for whitespace or chrome.
When changing the UI, hold this line:

- One thread = one compact line. No cards, no big padding, no wasted vertical space.
- Push detail into **hover** affordances, not always-on layout: the AI summary and the
  per-channel thread list appear on hover so the resting view stays dense.
  - Hover the **title** → detail panel: the thread's first message quoted and attributed
    to its author (the real "title"), then bullets summarizing the thread.
  - Hover the **#channel** handle → popover listing every thread in that channel, ranked;
    each listing is a `slack://` link into the desktop app.
  - Hover **`Nm`** → "N message(s)"; hover **`Np`** → "N people" (native tooltips).
  - The title hover also shows a one-line heat-metrics strip above the quoted first
    message: the overall score plus the factors that compose it, left to right. Glyph ->
    factor: 🌡️ overall heat score; 🏷️ channel weight (`×N.NN`); 📊 base (`Nm·Np`, message
    count times people count, with 👑 appended when a VIP-weighted person is in the
    thread); ⚡ velocity (replies/min; dimmed when `velocity_weight` is 0, since it then
    contributes nothing to the score); ⌛ time-alive (working hours, first-post ->
    last-post; dimmed when `alive_weight` is 0, its display-only default); ⏲
    time-since-last (working hours, last-post -> now; the atrophy input); ⏱️ recency
    (atrophy decay multiplier, 0..1); 👤 involvement damping (`×N.NN`). ⚡, 👤, and 👑 are
    the same glyphs the row already uses for its emoji column; the strip just reuses them
    rather than inventing new ones.
- Fixed-width left columns (dismiss `×`, counts `Nm|Np`) so titles align into a scannable
  column. Counts are intentionally terse (`3m|3p`, not "3 messages · 3 participants").
- Channel handles render Slack-style: lowercase, `#`-prefixed, no ALL-CAPS, no pill.
- Thread/channel links use the `slack://` scheme (desktop app) when `slack.team-id` is
  set; see `deep_link` / `channel_link` in `web.py`.

Before adding anything to a row, ask: does this earn its pixels, or can it be a hover?

## Build / test

```bash
otto ci          # whitespace + ruff format + ruff lint + mypy strict + pytest
uv run pytest    # tests only
```

## Run locally

Needs config at `~/.config/slack-dashboard/slack-dashboard.yml` and three env vars
(`SLACK_DASHBOARD_SLACK_USER_TOKEN`, `SLACK_DASHBOARD_SLACK_APP_TOKEN`, `SLACK_TEAM_ID`,
`ANTHROPIC_API_KEY`) decrypted from `scottidler/secrets` via `manifest age`:

```bash
eval "$(manifest age decrypt ~/repos/scottidler/secrets/.secrets)"
uv run slack-dashboard   # serves on server.host:server.port (currently 0.0.0.0:8889)
```

There is no global install step (run via `uv run` or the Dockerfile); `/shipit` should
skip install.

## Architecture

- `slack/poller.py` - REST backfill + ranking (`ranked_threads`), owns `threads` map.
- `slack/listener.py` - Socket Mode live events feeding the poller's queue.
- `connection.py` - disconnect/reconnect trust banner + reconcile arming.
- `heat.py` - heat score, `velocity`, `replies_in_window`, zombie/resurrection.
- `web.py` - FastAPI routes + `group_threads` (none/channel/size/velocity) + row/deep-link
  builders. Renders Jinja partials in `templates/partials/` (HTMX-driven).
- `llm/provider.py` - Anthropic title + bullet-summary generation.

## Conventions

Scott's global rules apply (`~/.claude/CLAUDE.md`): uv, ruff, mypy strict, kebab-case
config keys, no em dashes anywhere (including rendered UI text), `bump` for releases.
