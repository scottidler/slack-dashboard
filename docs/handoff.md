# Handoff: slack-dashboard prioritization & footprint work

**Date:** 2026-06-27
**Repo:** `/home/saidler/repos/scottidler/slack-dashboard` (GitHub `scottidler/slack-dashboard` - **PUBLIC**)
**Running app:** http://localhost:8889 (pid as of handoff: 2261940; launched detached)

## What this is
A single-pane Slack triage dashboard. This session focused on (a) making ranking actually
prioritize the user's orgs, and (b) reducing the 3-pages-of-scroll footprint. Full context,
decisions, panel review, and the phased plan are in the design doc - READ IT FIRST:

- `docs/design/2026-06-27-prioritization-and-footprint.md` (Status: Approved; decisions locked; review-panel findings folded in)
- Prior baseline: `docs/design/2026-06-21-ranking-and-triage-redesign.md` (v2, implemented)

## State of the work (ALL UNCOMMITTED in the working tree; CI green, 185 tests)
Done this session - see `git diff` for specifics, not reproduced here:
1. **Group-by filter persistence fix** (templates) - filter no longer reverts on the 30s poll.
2. **Phase 1 - participant key unified to Slack `user_id`** across all 3 write paths (`poller.py`,
   `listener.py` already did) - fixes a real double-count bug. Prerequisite for people-weights.
3. **Channel-view ordering** (`web.py group_threads`): order channel groups by channel weight,
   then **cluster by family** (first hyphen token, so all `sre-*` / `data-platform-*` sit
   together), families ordered by their strongest channel; hottest-thread as final tiebreak.
4. **Phase 2 - people-weights**: `config.HeatConfig.people_weights` (keyed by Slack user_id,
   glob-aware) + `people_weight_cap`; additive+bounded participant term in `heat.compute_heat`;
   `resolve_person_weight`; VIP crown emoji in `web._emojis` + legend in `templates/index.html`.
5. **Phase 3 - compact/full toggle (footprint fix)**: `config.DisplayConfig.compact_rows`
   (default 20, 0 disables the fold) + `display` block in the example yml. `web.py`: `RowView`
   gains `below_fold`; `group_threads` tags rows by **global heat rank** (`i >= compact_rows`),
   so the fold pins to a thread's rank whatever the grouping; `/threads` takes a `compact`
   query param and emits `total`/`hidden`/`limit`. `partials/threads.html`: `.disclosure`
   wrapper (`.compact` class), `.below-fold` rows, `.all-below-fold` group sections (so a
   group entirely below the fold doesn't show a stranded header), and the single global
   toggle ("+N more - show all M" / "show top N - collapse"), shown only when `hidden > 0`.
   `base.html` CSS hides the tail in compact mode via **pure CSS** (rows always in the DOM -
   zero-miss disclosure contract). `index.html`: `window.slackDashCompact` global re-sent via
   `hx-vals` (so the 30s poll preserves the selection) + a **delegated** click handler on the
   stable `#thread-list` (the toggle re-renders each poll). Tests: disclosure contract in
   `test_web.py` (all rows server-rendered, tail tagged, hidden count, full-mode class,
   no-toggle-under-fold) + fold-rank unit tests in `test_grouping.py`.

Verified live after restart: family clustering correct (sre cluster, then data-platform, then
ask-security...), 26 crowns rendering in the default view. Phase 3 verified live too: app boots
clean with the new `display` config (live yml has no `display` block, so default 20 applies),
wrapper class flips compact<->full correctly. The fold tail/toggle only appears with >20
tracked threads; the live workspace currently tracks 3, so the toggle correctly does not show -
the >20 behavior is covered by the new tests.

## Live config (PRIVATE - `~/.config/slack-dashboard/slack-dashboard.yml`, NOT in the repo)
- `heat.channel-weights` populated (sre/data-platform/security/incidents high; helpdesk=IT high;
  noise low). `data-platform*` nudged to 2.6 so its family clusters right after sre.
- `heat.people-weights`: 20 people resolved to Slack user_ids. `people-weight-cap: 30`.
- Draft reference: `~/.config/slack-dashboard/people-weights-draft.yml`.
- **2 people unresolved** (no Slack user_id - token lacks search scope); see the `~/.config`
  draft for who and their intended weights. Add their ids when known.

## NEXT: nothing required - Phases 1-3 done. Remaining options:
- **Ship the accumulated work** (`/shipit` ONLY when the user asks) - run `/code-review` first.
- **Tune `display.compact-rows`** in the live `~/.config` yml if 20 is the wrong fold size
  (off-repo; not committed). The fold won't be visible until >20 threads are tracked.
- **Deferred follow-ons** (per the design doc, not started): B2 collapsible channel groups
  (real view work, NOT free `/channel` popover reuse); Persona-backed people-weights behind
  the Phase 2 resolver; Spiking emoji (gated on velocity enabled); New emoji (needs a real
  `first_observed_at`); Unanswered emoji (needs NLP).

## CRITICAL gotchas (do not relearn the hard way)
- **NEVER run `manifest age decrypt` or touch the user's secrets.** They are already injected
  into every shell's env (verify presence with `printenv NAME`, never print values).
  `~/.claude/settings.json` (symlink -> `scottidler/claude/HOME/.claude/`) now has `ask` rules
  for `manifest*`.
- **Restart pattern that works:** kill old (`pkill -f '\.venv/bin/slack-dashboard'`), then a
  QUICK detached launch that returns immediately: `cd repo && nohup setsid uv run slack-dashboard
  > LOG 2>&1 < /dev/null & disown; echo $!`. Then a SEPARATE `curl --retry` health check.
  Combining kill+wait-loop+long-curl in one command gets killed by a sandbox signal (exit
  143/144). Config changes need a restart (loaded once at startup); template changes do not
  (Jinja auto_reload=True - just hard-refresh the browser).
- **PRIVACY (hard rule):** real names, emails, people-weights, and org tiers must NEVER be
  committed to this PUBLIC repo or any `tatari-tv` repo. They live only in `~/.config`
  (off-repo). The design doc is name-scrubbed (roles only); the example yml uses fake ids.
- **Commits:** nothing has been committed. Commit/ship only when the user explicitly asks.

## User org context
Captured in local memory `user_org_role.md` (role, management chain, teams, customer/partner
tiers). Not duplicated here to keep this file free of names.

## Suggested skills
- `/how-to-execute-a-plan` or `/rwl-a-plan` - to implement Phase 3 from the design doc (per-phase
  model tags are in the doc).
- `/code-review` - before shipping the accumulated uncommitted changes.
- `review-panel` agent (Implementation Audit mode) - to audit the Phase 1-2 implementation against
  the design doc and v2 invariants.
- `/shipit` - ONLY when the user asks; commits + bumps + pushes.
