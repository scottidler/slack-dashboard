# Implementation Notes: Ranking & Triage Redesign

Running, append-only record of how the implementation interprets or diverges from
`2026-06-21-ranking-and-triage-redesign.md`. Append once per phase; never edit
prior entries.

## Phase 1: Config + data-model scaffolding

### Design decisions
- `ThreadEntry` field name — `thread.py:ThreadEntry` — added `resurrection_event_ts: float = 0.0`
  rather than the `resurrected` boolean that Phase 1's bullet literally lists. The
  authoritative Data Model section (doc lines 79-86), the resurrection design (line 111),
  and Phase 2 all specify storing `resurrection_event_ts` and computing the zombie flag
  at rank time ("`resurrected` is computed, not sticky"). Phase 1's one-word "`resurrected`"
  is an internal inconsistency in the doc; followed the authoritative spec.
- Config field placement — `config.py` — put velocity/resurrection/decay/channel-weight
  knobs on `HeatConfig` (they feed the heat formula), `channel_min_replies` on `FetchConfig`
  (it scopes fetching, alongside `min_replies`), and `workspace` at `AppConfig` top level
  (it is a deep-link concern, not heat or fetch).
- Glob resolution — `config.py:resolve_channel_weight` / `resolve_min_replies` — exact key
  wins; otherwise the first matching glob in sorted-key order (deterministic when multiple
  globs match); default to the neutral value (`1.0` weight, global `min_replies`) if none match.
- decay rename backward-compat — `config.py:HeatConfig` — a `model_validator(mode="before")`
  maps a legacy `decay-half-life-hours` / `decay_half_life_hours` key onto `decay-hours`
  when the new key is absent, so existing config files keep loading.
- Default knob values — `velocity_weight=0.0` and `channel_weights={}` keep ranking
  byte-for-byte identical to today (neutral weights, zero velocity contribution), per the
  rollout plan. Picked `velocity_window_minutes=30`, `resurrection_gap_hours=24`,
  `resurrection_age_days=2`, `resurrection_display_hours=24` as starting points (open
  questions in the doc); they only matter once velocity_weight/channel_weights are tuned up.

### Deviations
- See the `resurrection_event_ts` vs. `resurrected` decision above — a deliberate departure
  from Phase 1's literal wording in favor of the rest of the doc.

### Tradeoffs
- `model_validator` for the decay alias vs. pydantic `AliasChoices` — chose the validator
  because the kebab `alias_generator` already occupies the `alias` slot, and a before-validator
  is the least surprising way to accept a second legacy spelling without fighting the generator.

### Open questions
- None blocking. Exact resurrection/velocity starting values remain doc open-questions to
  tune after the formula lands (Phase 2/5).

## Phase 2: Heat formula v2

### Design decisions
- `first_seen_ts` is set deterministically from `float(thread_ts)` in all write paths
  (`poller.py:_fetch_thread`, `listener.py:_apply_event`) rather than carried forward.
  `thread_ts` IS the parent message timestamp, so first-seen is always reconstructible and
  can never be lost by a rebuild — simpler than the carry-forward the contract listed for it.
- `resurrection_event_ts` IS carried forward in the full-fetch rebuild (`poller.py:_fetch_thread`)
  and is never recomputed there. The gap is captured only in the listener and the incremental
  path, before `last_activity` is overwritten; the rebuild runs after the listener already
  bumped `last_activity`, so recomputing the gap there would always read ~0. Carrying forward
  is the only correct behavior for that path.
- `reply_timestamps` in the full rebuild = `prune_timestamps(existing + fetched)`. The fetched
  replies are the source of truth for a full fetch, but carried-forward entries are merged so a
  socket-appended timestamp that predates the fetch window boundary isn't dropped before pruning.
- Velocity is read-only in `compute_heat`; pruning/mutation happens at update time in the poller
  and listener via `prune_timestamps`, keeping `compute_heat` a pure function.
- `MAX_REPLY_TIMESTAMPS = 500` (`heat.py`) is the hard cap backing the doc's "oldest dropped
  past a hard max"; combined with window pruning it bounds per-thread memory.
- `SocketListener` gained a `heat_config: HeatConfig` constructor param (wired in `main.py`)
  so it can run resurrection detection and pruning at the event boundary.

### Deviations
- None from the Phase 2 spec. (`first_seen_ts` derivation is a simplification of, not a
  departure from, the State merge contract — same observable result.)

### Tradeoffs
- Carry-forward + merge for `reply_timestamps` vs. reconstruct-from-fetch-only — chose merge to
  honor the doc's explicit "carry forward" contract and make the regression test meaningful,
  accepting a tiny dedup-less overlap that pruning collapses anyway.

### Open questions
- Velocity/resurrection starting constants remain doc open-questions; defaults are tuned to be
  inert (velocity_weight=0) until Scott raises them in the Phase 5 tuning pass.

## Phase 3: Dismiss persistence

### Design decisions
- Dismiss path — `main.py:_resolve_dismiss_path` — the JSONL store lives at
  `dismissed.jsonl` alongside the config (same dir as `slack-dashboard.yml`). No new
  config key; it is derived, keeping the surface small.
- Poller method named `dismiss_thread` (not `dismiss`) to avoid colliding with the
  `_dismiss` store attribute and to read clearly at the call site (`poller.dismiss_thread(...)`).
- Eviction — `poller.py:_evict_threads`, called once per refresh tick before re-seeding —
  removes dismissed keys and dead threads (`last_activity` past `max_thread_age_days`) that are
  not currently `is_zombie`. This is the doc's required eviction step; without it `self._threads`
  never shrinks. A re-`del` of a zombie is avoided so a resurrected thread keeps rendering.
- Belt-and-suspenders: `ranked_threads()` also filters the dismissed set even though
  `dismiss_thread` already evicts and `_fetch_thread` short-circuits before fetching, so a
  dismissed thread can never reappear via any path between refreshes.

### Deviations
- None.

### Tradeoffs
- `os.fsync` on every dismiss vs. buffered append — chose fsync for crash-durability of the
  one piece of state that must survive a restart; dismisses are rare (manual, single-user) so
  the sync cost is irrelevant.

### Open questions
- Undo/tombstone is intentionally not built (doc open question); the append-only `status`
  discriminator leaves the door open to add it without migration.

## Phase 4: Compact-row UI

### Design decisions
- View-model in Python — `web.py:RowView` / `GroupView` / `group_threads` / `_build_row` /
  `deep_link` — emoji, zombie computation, deep-link construction, and grouping live in
  testable functions; the template only iterates and renders. This keeps `is_zombie`/workspace
  logic out of Jinja and unit-testable without a TestClient.
- `create_routes` gained a `config: AppConfig` param (wired in `main.py`) so the route can build
  deep links (workspace) and compute zombie state (heat config). Cleaner than exposing a
  `poller.config` accessor that the AsyncMock-based web tests couldn't satisfy.
- Counts rendered compactly as `{n}r · {n}p` (replies/participants) per the one-line row spec.
- Emoji: zombie (when `is_zombie`) then fire (when `heat_tier == "hot"`). Hot tier already
  folds in velocity, so "hot tier / high velocity" collapses to the tier check.
- `group-by` accepted via `Query(..., alias="group-by")` to honor the kebab API param while
  keeping a snake_case Python name.

### Deviations
- group-by size/velocity/participants are implemented as a single ordered group sorted by that
  dimension (descending), NOT as bucketed sub-groups. The doc lists these modes but bucketing a
  continuous metric is fuzzy and risks the scope creep the doc warns against. Only `channel`
  produces true per-group partitioning in v1. Every thread still renders (zero-miss intact).

### Tradeoffs
- Hover-to-summarize via `hx-trigger="mouseenter once"` on the row + CSS `:hover` reveal vs. an
  always-visible summary — chose hover to keep rows to one line (density), fetching the summary
  lazily on first hover.

### Open questions
- Default `group-by` is `channel` (doc open question); easy to flip to size/velocity later.
- `slack://` app-protocol deep links vs. the web URL remains a doc open question; shipped the
  web URL (`deep_link`), which works from both desk and laptop.

## Phase 5: Tests, docs, tuning pass

### Design decisions
- Wired the per-channel `min_replies` resolution into the poller here
  (`poller.py:_fetch_channel` now calls `resolve_min_replies(channel_name, fetch)`).
  This was a Phase 1 functional requirement that the Phase 1 commit added the config and
  resolver for but didn't connect at the fetch call site; corrected in this phase with a
  dedicated test (`test_fetch_channel_uses_per_channel_min_replies`).
- End-to-end test (`tests/test_e2e.py`) drives a real `SlackPoller` + `DismissStore` +
  `create_routes` through a `TestClient`: backfill -> render compact row + deep link ->
  dismiss -> reload store in a fresh poller and assert the dismissal survives the "restart"
  and short-circuits the fetch. This covers the doc's render test, dismiss-persistence-across-
  restart, and deep-link-format requirements in one realistic flow.
- README rewritten from a stub to document the ranking formula, the dead/dismissed distinction,
  every new knob, and the UI; added a fully-commented `slack-dashboard.example.yml` with sane
  starting weights (watchlist channels at 2.0, `proj-*` at 0.5, velocity off by default).

### Deviations
- None.

### Tradeoffs
- One integration-style e2e test vs. several smaller route+persistence tests - chose the single
  realistic flow for the headline guarantees, leaving fine-grained behavior to the unit tests
  already added in Phases 1-4.

### Open questions
- Remaining doc open questions (default group-by, decay shape, velocity starting values,
  dismiss undo, config hot-reload, per-signal rank observability) are deferred tuning/v2
  decisions, not implementation blockers.
