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

## Post-review corrections (2026-06-21)

External audit (Architect/Gemini + Staff Engineer/Codex), consensus round folded into the
design doc's "Post-Implementation Review" section. Corrections to earlier notes:

- **Phase 2 tradeoff note was wrong.** The claim that the carried-forward + fetched
  `reply_timestamps` overlap is "a tiny dedup-less overlap that pruning collapses anyway" is
  false: `prune_timestamps` sorts and caps but does not deduplicate, so socket+REST race
  double-counts each live reply and inflates velocity once `velocity_weight > 0`. Agreed fix:
  store raw `float(ts)` in the listener and dedup by a normalized Slack-ts key in
  `prune_timestamps`.
- **Resurrection eviction interaction is a real bug, not just a restart limitation.** The
  Phase 3 `_evict_threads` step removes the prior state that resurrection detection relied on,
  so the headline zombie feature does not fire for its main case. Agreed fix: reconstruct the
  quiet-gap from the full-fetch reply timestamps (state-independent), superseding the Phase 2
  "carry-forward only" decision for the full-fetch path.

These remedies are recorded as "Confirmed bugs to fix" in the design doc; they are not yet
implemented in code.

## Phase 6: Post-review fix batch

### Design decisions
- **Velocity dedup by normalized key** (`heat.py:prune_timestamps`) using `f"{ts:.6f}"`, not
  exact-float `set()` (Codex's refinement). The listener now stores raw `float(ts)`
  (`listener.py:_apply_event`) so both paths agree; the normalized key is belt-and-suspenders
  against any residual float drift.
- **Resurrection reconstruction** (`heat.py:reconstruct_resurrection`, used in
  `poller.py:_fetch_thread`): scan the full fetched reply timeline for the most recent adjacent
  gap exceeding `resurrection_gap_hours`; the reply ending that gap is the event. State-independent,
  so it fires for evicted and across-restart threads. `is_zombie` still gates display on age +
  recency, so reconstruction does not re-check age.
- **Per-thread watermark = the existing `_thread_watermarks`.** No new field was needed; that dict
  already holds the max reply ts fetched per thread, which is exactly the "latest reply I have"
  pointer the reconcile compares against.
- **Reconnect detection by polling `is_connected()`** (`main.py:_connection_monitor`). Verified
  against slack_sdk 3.41.0: `SocketModeClient` exposes `on_close_listeners` (disconnect edge) and
  `is_connected()`, but NO on-connect callback - so the disconnect flips the banner immediately via
  `on_close`, and a 5s poll detects the reconnect edge and triggers `poller.reconcile()`.
- **Banner** via a `/status` partial the HTMX shell polls every 10s (`index.html`,
  `partials/status.html`); `ConnectionState.status()` returns connected/disconnected/disabled.

### Deviations
- Minor: in the full-fetch path, when reconstruction returns 0.0 (no gap in the fetched window)
  but an `existing` entry carried a resurrection event, the existing value is kept as a fallback
  (`poller.py:_fetch_thread`). This guards incremental-built entries whose sparse `reply_timestamps`
  can't reconstruct the gap; it never overrides a reconstructed event.

### Tradeoffs
- 5s connection poll vs. a callback: slack_sdk gives no on-connect hook, so polling is the
  pragmatic correct choice. The disconnect edge is still immediate (via `on_close_listeners`).
- Reconcile re-lists each channel with `oldest=None` (full recent history) rather than a watermark,
  because the whole point is to catch old parents the watermark would skip; it then re-fetches only
  the threads whose `latest_reply` moved, keeping replies-fetch cost proportional to actual change.

### Open questions
- Thread-listing lookback depth bounds how far back a missed reply can be recovered (documented
  caveat in the doc's Zero-miss "Discovery caveat"); deep pagination deferred.

## Phase 6 follow-up: reconnect-race fix (post-verification)

The Phase 6 verification round (Architect + Staff Engineer) confirmed fixes 1-3 and the reconcile
logic, but Codex caught a real bug in the reconnect *trigger* that the Architect missed:

- **Reconnect-race fix.** The original monitor decided reconcile off a private `was_connected`
  derived from the 5s `is_connected()` poll, while `_on_close` updated a separate field - so a
  disconnect+reconnect entirely between two polls was never reconciled (the poll only ever saw
  "connected"). Fixed by composing both edges through `ConnectionState.reconcile_pending`:
  `_on_close` calls `mark_disconnected()` (arms the flag); the monitor calls `observe(connected)`,
  which fires reconcile on the first `connected` poll while the flag is armed, then clears it.
  Driven by the reliable on_close edge, not poll timing. Regression tests in `test_connection.py`
  cover the short-disconnect, fire-once, and pending-survives-polls cases.
- **Monitor hardening.** The monitor loop body is now wrapped so a transient `is_connected()`
  (or other) exception logs and continues instead of killing the task (which would silence all
  future reconnect catch-up). `CancelledError` is re-raised for clean shutdown.

Not changed (accepted): the reviewers' "reconnect cost" RISKY note - reconcile lists every channel
with `oldest=None` on reconnect, but `client.py`'s history semaphore + 1.2s sleep serializes those
calls under the rate ceiling, so it is bounded latency, not a rate-limit violation. The deep-
pagination lookback caveat remains documented in the Zero-miss "Discovery caveat".

## Phase 6 follow-up: test coverage for the integration seams

Closed the three coverage gaps surfaced when asked "do we have tests to prove this works?":

- **Discovery-hole proof** (`test_reconcile_refetches_old_parent_with_new_reply`): a KNOWN old
  parent whose `latest_reply` advanced past its watermark is re-fetched on reconcile - the literal
  case the feature exists for, previously only covered via the new-thread path.
- **Cross-component velocity dedup** (`test_velocity_not_double_counted_across_listener_and_fetch`):
  drives `listener._apply_event` then `poller._fetch_thread` on the same reply and asserts one
  timestamp survives, not two.
- **Monitor loop glue**: extracted the connection monitor from `main.py` into a typed, testable
  `connection.monitor_connection(...)`. Two tests: reconcile fires once on the reconnect edge, and
  the loop survives a transient `is_connected()` exception and still reconciles.

The extraction immediately caught a latent bug mypy had been unable to see while the loop was a
closure: `SocketModeClient.is_connected` is an **async** method, so the original `bool(is_connected())`
coerced a coroutine (always truthy) - it would have cleared the disconnect banner on the next poll
and never awaited. `monitor_connection` now takes `Callable[[], Awaitable[bool]]` and awaits it.

Still not unit-tested (genuine glue / external): the one line registering `_on_close` on the real
`SocketModeClient`, and a real Socket Mode disconnect (requires Slack). Both halves each connects
are covered.
