# Design Document: Emoji State Signals & Observation Store (Triage v3.1)

**Author:** Scott Idler
**Date:** 2026-06-27
**Status:** Implemented (2026-06-28; Phases 1-4 landed, CI green at 217 tests; Design Review
complete 2026-06-27; panel blockers B1/B2 + missing-decisions M1/M2 folded in; author
decisions locked)
**Review Passes Completed:** 5/5 + cross-model panel
**Builds on:** `2026-06-21-ranking-and-triage-redesign.md` (v2, implemented) and
`2026-06-27-prioritization-and-footprint.md` (v3, Phases 0-3 implemented). This doc picks up
v3's explicit **Deferred** list.

## Summary

v3 expanded the emoji state channel (added the VIP crown alongside fire and zombie) and
deferred three more single-glyph signals: **Spiking** (happening right now), **New** (just
appeared in your view), and **Unanswered** (a question nobody has answered). This doc designs
all three. Two are cheap render-time computations; the third (**New**) needs something the app
does not have - a record of *when the dashboard first observed a thread* - so the bulk of this
design is a small **sqlite3-backed observation store** that supplies it and survives restart.
The v1 semantic is precise: ✨ means *"entered the dashboard's view within the last
`new_window` minutes,"* which is a deliberate proxy for "new since I last looked" - not the
literal thing (panel C1). The poller runs continuously, so a thread that arrived while the user
was away longer than the window will not carry ✨ on return; the literal semantic needs a
last-viewed watermark (Alternative 5), deferred. The proxy is the v1 to validate by observation.

## Problem Statement

### Background

The emoji state channel (`web._emojis`, `web.py:113`) is a high-density signal surface: each
glyph costs ~one character of width and conveys a thread's state at a glance. v3 ships three
glyphs, all computed at render time from already-present data:

- `:zombie:` (🧟) - `is_zombie` (`heat.py:120`): old thread, went quiet, new activity.
- `:fire:` (🔥) - `heat_tier == "hot"`.
- `:crown:` (👑) - `_has_vip` (`web.py:107`): a participant carries an above-default
  people-weight.

v3's "Cross-cutting: a richer emoji state channel" section proposed three more and its review
panel (Q6) resolved to defer them. Their gating reasons, re-verified against the code:

- **Spiking** - velocity is off in the score (`velocity_weight: 0.0`, `config.py:54`); the doc
  did not want a "live spike" glyph while the ranking ignored velocity, and wanted it visually
  distinct from fire.
- **New** - `first_seen_ts` is set to `float(thread_ts)` in every write path (`poller.py:314`,
  `poller.py:260`, `listener.py:99`). `thread_ts` is the thread's *creation* time, not when
  the dashboard first saw it, so it cannot distinguish "newly created" from "new to us." No
  observation timestamp exists.
- **Unanswered** - rejected as needing NLP to tell a question from a statement and a real
  answer from "thanks"/a reaction.

### Problem

1. **Two high-value glyphs are computable today but unshipped.** Spiking re-uses
   `replies_in_window` (`heat.py:38`), already proven by the `velocity` group-by mode. The
   only real work is enabling velocity in the score and choosing a distinct glyph.
2. **"New" is blocked on a missing capability, not a missing glyph.** The signal a triage user
   actually wants is *"what showed up since I last looked"* - and heat cannot express it (a
   brand-new 3-reply thread has low heat). Delivering it across restarts requires persisting an
   observation timestamp, which is the app's first piece of derived on-disk state beyond the
   dismiss log.
3. **"Unanswered" is the most useful of the three but the riskiest.** It is *counter-correlated
   with heat*: an unanswered question has little activity, so the heat ranking actively buries
   exactly the dropped-ball threads in the channels the user owns (e.g. `#ask-security`,
   `#it-helpdesk`). That orthogonality is the value; reliability is the risk.

### Goals

- Ship **Spiking** and **New** as first-class glyphs in the existing `_emojis` channel, with a
  legend entry each (v3 owes a legend at 3+ glyphs; we are well past that).
- Give the app a durable, bounded **observation store** so "New" means *recently entered the
  dashboard's view* (within `new_window`) and holds across restarts - without ever touching the
  ranking/render hot path. (Proxy for "since I last looked"; see Summary and Alternative 5.)
- Keep every glyph **explainable** and computed at render time from local state (the v2/v3
  principle), with the sole exception of the optional LLM-judged Unanswered variant.
- Offer **Unanswered** as an explicitly optional, arithmetic-first experiment, so the user can
  try the cheap proxy before deciding whether an LLM pass earns its keep.

### Non-Goals

- **No change to the heat formula's shape.** Spiking turns on the *existing* velocity term
  (`vel * velocity_weight`, `heat.py:68`); it does not add a new term.
- **No general-purpose persistence framework.** The observation store does one thing: map a
  thread key to its first-observed timestamp. It is not a thread cache or an event log.
- **No weakening of zero-miss.** Glyphs annotate rows; they never hide or drop them.
- **No new external runtime dependency.** sqlite3 is stdlib; nothing is added to `pyproject`.

## Proposed Solution

### Overview

Three independent slices, ordered cheapest-first:

1. **Spiking glyph** (⚡) - render-time, from `replies_in_window >= spiking_threshold`. Enable
   the velocity term in the live config so the score and the glyph agree.
2. **Observation store** (sqlite3) + **`first_observed_at`** on `ThreadEntry` - the
   architectural piece. Stamps each thread once, persists it, prunes it, degrades gracefully.
3. **New glyph** (✨) - render-time, from `now - first_observed_at < new_window` (and not a
   zombie). Thin layer on top of slice 2. Note this is a *time-window* proxy for "new to your
   view" - a thread is new for `new_window` after it appears, not strictly "since your last
   visit" (see Alternative 5 for the truer-but-heavier last-viewed-watermark variant).

Plus an optional fourth:

4. **Unanswered glyph** (❓) - arithmetic proxy first (`?` in first message + low reply count +
   aged), LLM-judged variant deferred behind the same render-time predicate.

### Architecture

```
                         ┌─────────────────────────────────────────┐
   Slack REST/Socket ───▶│ poller._fetch_thread  (poller.py:299)    │
                         │   the ONE thread-creation chokepoint      │
                         │   first_observed_at = observed.stamp(key) │
                         └───────────────┬───────────────────────────┘
                                         │ read-or-write-once
                         ┌───────────────▼───────────────┐
                         │ ObservedStore (observed.py)     │
                         │  • in-memory dict (read mirror) │◀── load() at startup
                         │  • sqlite3 (durability only)    │──▶ prune() on each backfill
                         └─────────────────────────────────┘
                                         │ in-memory O(1)
   ranked_threads ──▶ _build_row ──▶ _emojis(thread, config)   (render hot path: NO I/O)
                                         │
                          ⚡ spiking · ✨ new · 👑 vip · 🔥 fire · 🧟 zombie
```

The store mirrors the **DismissStore wiring** exactly (`main.py:64`): construct in `_build_app`,
call `.load()`, pass into `SlackPoller(observed=...)`. The hot path (`ranked_threads` →
`_build_row` → `_emojis`) reads only the in-memory dict; sqlite is touched on thread creation
(one tiny write) and on prune. This is the same hot-path rule v3 set for Persona (Blocker 3):
durability layer, never the read path.

### Why sqlite3 rather than another JSONL store

`DismissStore` (`dismiss.py`) is append-only JSONL and its docstring calls dismiss "the only
state that must survive a restart." Adding sqlite is a deliberate divergence, justified by a
different data lifecycle:

| | DismissStore (JSONL) | ObservedStore (sqlite3) |
|---|---|---|
| Write pattern | append, monotonic | write-once-per-key (`INSERT OR IGNORE`) |
| Size | tiny (handful of dismissals) | one row per thread *ever seen* |
| Pruning | never (you don't un-dismiss) | **required** - bounded by `max_thread_age_days` |
| Prune op | n/a | `DELETE WHERE first_observed < ?` (indexed, atomic) |

The decisive factor is **pruning**. Append-only JSONL works for dismiss precisely because it
never prunes. Observed state grows with every thread and must be bounded; doing that in JSONL
means periodic compaction (read-all → filter → rewrite temp → rename), which is exactly the
fragile full-file rewrite that `DismissStore`'s fsync-append avoids. sqlite gives atomic
write-once and indexed delete with no rewrite race. (JSONL-with-compaction is evaluated and
rejected under Alternatives.)

### Data Model

`ThreadEntry` (`thread.py:5`) gains one field, mirroring `first_seen_ts`:

```python
first_observed_at: float = 0.0  # wall-clock epoch the dashboard FIRST saw this thread
                                # (observation time, not thread creation time); from ObservedStore
```

sqlite schema (one table, composite PK matching the in-memory thread key):

```sql
CREATE TABLE IF NOT EXISTS observed (
    channel_id    TEXT NOT NULL,
    thread_ts     TEXT NOT NULL,
    first_observed REAL NOT NULL,   -- epoch seconds
    PRIMARY KEY (channel_id, thread_ts)
);
CREATE INDEX IF NOT EXISTS idx_observed_first ON observed (first_observed);
```

The composite PK makes `INSERT OR IGNORE` the write-once primitive; the index makes pruning a
single indexed `DELETE`.

### API Design

```python
# observed.py
class ObservedStore:
    def __init__(self, path: Path) -> None: ...
    def load(self) -> None:
        """Open/create the db and hydrate the in-memory mirror. On any sqlite error,
        log WARN and fall back to in-memory-only (degraded) mode - never raise."""
    def stamp(self, channel_id: str, thread_ts: str, now: float) -> float:
        """Return the thread's first-observed epoch, writing `now` once if unseen.
        Reads are mirror-only (the mirror is a complete reflection of the db after
        load(), so a hit never touches sqlite); a miss does INSERT OR IGNORE and
        updates the mirror. Any sqlite error (e.g. 'database is locked') is trapped,
        logged WARN, and the mirror value is returned/used - stamp() NEVER raises, so a
        write failure can never crash the poller worker (_process_item). In degraded mode
        (no db) the mirror is per-session only."""
    def delete(self, keys: Iterable[tuple[str, str]]) -> int:
        """Drop the given (channel_id, thread_ts) rows and mirror entries. Driven by the
        EXACT set _evict_threads removes (a targeted indexed DELETE), so the store tracks
        the in-memory horizon by last_activity - NOT by a static first_observed age, which
        would purge long-lived *active* threads (old first_observed, recent last_activity)
        and re-stamp them as falsely New on the next event (Blocker B1). Errors degrade,
        never raise. Returns count."""
```

Connection is opened and used on the poller's event-loop thread (no executor offload), so the
default `check_same_thread=True` is safe. A deliberately low `busy_timeout` is set so a locked
db fails fast into the trap-and-degrade path rather than stalling the loop.

Poller integration at the single creation chokepoint (`poller.py:299`), where the existing
code already reads prior state off `existing` for the rebuild:

```python
first_observed_at = self._observed.stamp(channel_id, thread_ts, datetime.now(UTC).timestamp()) \
    if self._observed else float(thread_ts)
entry = ThreadEntry(..., first_observed_at=first_observed_at)
```

`_emojis` (`web.py:113`) gains two predicates, ordered for scannability:

```python
_NEW = "\N{SPARKLES}"                       # ✨ new to your view
_SPIKING = "\N{HIGH VOLTAGE SIGN}"          # ⚡ spiking now
_UNANSWERED = "\N{BLACK QUESTION MARK ORNAMENT}"  # ❓ (Phase 4, opt-in)
# glyph order in the row: new, vip, spiking, fire, zombie (unanswered, when on, leads)
```

All glyph-tuning knobs live on `HeatConfig`, because `_emojis` already resolves its predicates
through `config.heat` (`is_zombie(thread, config.heat)`, `replies_in_window(thread,
config.heat)`). Keeping `spiking_threshold`, `new_window_minutes`, and the Phase-4 unanswered
knobs there keeps every glyph threshold in one place rather than splitting them across
`HeatConfig` and `DisplayConfig`.

### Implementation Plan

#### Phase 1: Spiking glyph
**Model:** sonnet
- `config.HeatConfig`: add `spiking_threshold: int = 15` (replies-in-window to count as
  spiking; chosen to align with the velocity group-by `spiking (15+)` tier so the glyph and
  that grouping agree).
- `web._emojis`: append ⚡ when `replies_in_window(thread, config.heat) >= spiking_threshold`,
  placed to read distinctly from 🔥.
- Legend in `index.html`: add the ⚡ entry.
- Example yml: set `velocity-weight` to a **materially** non-zero value (panel Q1: a token
  `> 0` is not enough; it must be large enough to actually move the score) with a comment that
  the glyph and the score now agree (the real value is an off-repo `~/.config` tune).
- Tests: glyph fires at/above threshold, absent below, distinct from fire.

#### Phase 2: Observation store (sqlite3) + `first_observed_at`
**Model:** opus
- New `observed.py`: `ObservedStore` per the API above (sqlite3 + in-memory mirror + graceful
  degrade on every path, low busy timeout).
- `thread.py`: add `first_observed_at` field.
- `poller.py`: accept `observed: ObservedStore | None`; stamp at the `_fetch_thread` creation
  chokepoint (`poller.py:299`). **Have `_evict_threads` collect the exact `to_evict` key list
  it already builds (`poller.py:96`) and pass it to `ObservedStore.delete(keys)`** so the store
  is pruned by the same `last_activity` horizon the in-memory map uses - NOT by a static
  first-observed age query (Blocker B1). This runs on the existing `_refresh_loop` cadence
  (`poller.py:192`).
- `main.py`: `_resolve_observed_path()` (alongside `dismissed.jsonl`); construct, `.load()`,
  pass into the poller - mirroring DismissStore wiring.
- Tests: a second store over the same `tmp_path` db sees the original timestamp (restart
  survival); `INSERT OR IGNORE` does not clobber; `delete(keys)` drops exactly the evicted keys
  and leaves long-lived active threads intact (B1 regression); a `stamp()`/`delete()` call
  against a locked/unwritable db degrades without raising; hot path does no sqlite I/O.

#### Phase 3: New glyph
**Model:** sonnet
- `config.HeatConfig`: add `new_window_minutes: int = 60` (see glyph-knob rationale above;
  60 min is long enough to survive a coffee-break gap, short enough not to read as stale).
- `web._emojis`: append ✨ when `first_observed_at > 0 and now - first_observed_at < new_window
  and not is_zombie(thread, config.heat)`. The **`not is_zombie` guard is necessary but not
  sufficient on its own** (panel B2): zombie state is itself time-bounded
  (`resurrection_display_hours`), so a revived-but-still-active old thread whose zombie window
  has expired could re-read as New on a re-stamped row. The guard closes the resurrection case
  **only in combination with the B1 fix** (deleting observed rows by the eviction horizon, not
  a static age, so an active thread's row is never purged-then-restamped in the first place).
- **App-start storm suppressor (panel M2):** also gate the predicate on
  `now - app_start_at >= new_window`. This kills both the one-time empty-db storm AND the
  every-restart storm a *permanently* unwritable db would otherwise cause in degraded mode
  (degraded mode stamps everything `now` on each backfill). `app_start_at` is captured once at
  poller start and read at render time. (This also stands in for the warmup-grace idea deferred
  to Monday - it is the cheap, always-on version.)
- Legend in `index.html`: add the ✨ entry.
- Tests: glyph fires inside the window, absent outside, absent when `first_observed_at == 0`
  (degraded/unknown), absent when the thread is a zombie even if inside the window, absent for
  every thread within `new_window` of `app_start_at` (storm suppressed).

#### Phase 4 (optional, experimental): Unanswered proxy glyph
**Model:** sonnet
- `config.HeatConfig`: `unanswered_max_replies: int = 2`, `unanswered_min_age_hours: int = 2`,
  and `unanswered_enabled: bool = False` (opt-in; ships disabled, flipped on in the private
  `~/.config` for Monday observation).
- `web._emojis`: append ❓ when enabled and `first_message` ends with/contains `?` and
  `reply_count <= unanswered_max_replies` and the thread is older than the age floor.
- **Known proxy limitation (for the panel):** the per-channel `min-replies` floor interacts
  with `unanswered_max_replies`. In standard channels (`min-replies: 3`) a thread needs 3+
  replies just to appear, so `max_replies: 2` can never fire there; the glyph effectively only
  works in the ops channels running `channel-min-replies: 1` (sre, data-platform, incidents,
  ask-security) - which is, conveniently, exactly where an unanswered ask matters most. Raising
  `max_replies` to cover standard channels trades coverage for false positives.
- Legend entry; tests for the proxy predicate.
- The LLM-judged variant is documented but **not built**: it would set an `unanswered` flag via
  a low-priority background LLM pass (re-using `llm/provider.py`), read at render time by the
  same predicate - keeping the hot path arithmetic.

## Alternatives Considered

### Alternative 1: In-memory observation time + startup warmup grace (no persistence)
- **Description:** Stamp `first_observed_at = now()` in memory; record `warm_at` when the
  initial backfill drains; "New" fires only for threads first seen after `warm_at`. Threads
  discovered during backfill are never "new," so a restart causes no false-new storm.
- **Pros:** Zero new on-disk state; keeps "dismiss is the only persisted state" true; trivial.
- **Cons:** Cannot deliver "new since I last looked" - a thread that appeared while the app was
  down is invisible as new after restart. The user explicitly wants the across-restart
  behavior, which only persistence provides.
- **Why not chosen:** Misses the exact behavior that motivates the feature. (Retained as the
  graceful-degradation fallback when sqlite is unavailable.)

### Alternative 2: Mirror DismissStore - a second append-only JSONL with compaction
- **Description:** Append `{channel_id, thread_ts, first_observed}` records; load-all at
  startup; compact (rewrite) periodically to bound size.
- **Pros:** One persistence idiom across the app; human-readable.
- **Cons:** Observed state needs pruning, so it needs compaction, which is a read-all/rewrite/
  rename race - the fragile pattern fsync-append was chosen to avoid. More code than sqlite's
  one-line indexed DELETE; file grows unbounded between compactions.
- **Why not chosen:** The pruning requirement is exactly what JSONL handles worst and sqlite
  handles best (see "Why sqlite3").

### Alternative 3: Approximate "New" from `thread_ts` (creation time)
- **Description:** "New" = thread created within the last N minutes (`first_seen_ts`).
- **Pros:** No new state; restart-safe for free (pure Slack data).
- **Cons:** "Newly created," not "new to you" - wrong for a thread created long ago that only
  now crossed `min-replies` and entered the view. Conflates the two the v3 doc explicitly
  warned about.
- **Why not chosen:** Wrong semantics. (Mentioned as the absolute-cheapest option if the user
  ever wants to drop persistence entirely.)

### Alternative 4: LLM-judged Unanswered as the v1
- **Description:** Classify question-ness and answered-ness with the existing LLM provider.
- **Pros:** Highest accuracy; catches rhetorical/answered cases the proxy misfires on.
- **Cons:** False positives erode glyph trust fast; it is a different *class* of signal than
  the explainable arithmetic the channel is built on.
- **Why not chosen (as v1):** Trust risk. Ship the arithmetic proxy first; adopt the LLM
  variant only if the proxy proves the feature is wanted and the proxy is too noisy.

### Alternative 5: Last-viewed watermark instead of a first-observed window
- **Description:** Persist a "last viewed" timestamp (bumped when the dashboard is opened/
  focused) and flag every thread whose `first_observed_at` is newer than it - literally "new
  since I last looked," with no time window.
- **Pros:** Matches the phrase that motivated the feature exactly; no arbitrary `new_window`;
  an overnight gap correctly surfaces everything that arrived.
- **Cons:** Needs view-state tracking (what counts as "looked"? page load? tab focus? a
  per-client concern in a single-page HTMX app with a 30s poll), and a "mark seen" write path.
  More surface, and ambiguous with multiple browser tabs.
- **Why not chosen (for v1):** The first-observed + window proxy delivers most of the value
  with a fraction of the surface and no client/session state. The store designed here
  (`first_observed_at` per thread) is the substrate a last-viewed variant would also need, so
  this is an additive future step, not a rewrite.

## Technical Considerations

### Dependencies
- **sqlite3** - Python standard library; no `pyproject` change.
- Internal: `ThreadEntry`, `SlackPoller`, `main._build_app` wiring; the `replies_in_window`
  helper (already used by the velocity group-by mode).

### Performance
- Hot path (`ranked_threads`/`_emojis`) does **zero** sqlite I/O - all reads hit the in-memory
  mirror, same O(1) cost as the existing fire/zombie/crown checks.
- sqlite writes are one tiny `INSERT OR IGNORE` per *new* thread (rare relative to renders).
- Prune is one indexed `DELETE`, run on the existing eviction cadence, not per request.
- **Concurrency:** the poller's fetch workers are asyncio tasks on the single event-loop
  thread (no executor offload), so one sqlite connection with all access on the loop is safe -
  no `check_same_thread` hazard. The sync write briefly occupies the loop, but it is a
  single-row insert; no `run_in_executor` is warranted.

### Security / Privacy
- The db stores only `(channel_id, thread_ts, epoch)` - no message text, no names, no
  people-weights. It carries nothing privacy-sensitive and nothing that must stay off the
  public repo. (It is machine state and is not committed regardless.)

### Storage location
- Follow the DismissStore convention: alongside config at
  `~/.config/slack-dashboard/observed.db` (via a `_resolve_observed_path` mirroring
  `_resolve_dismiss_path`, honoring `XDG_CONFIG_HOME`). XDG-correct would be
  `~/.local/state/`, but consistency with the existing dismiss log wins; revisit only if a
  state-dir migration is done for both.

### Testing Strategy
- Unit: `ObservedStore` over `tmp_path` (restart survival, write-once, `delete(keys)`, degraded
  mode on stamp/delete, not just load).
- Unit: `_emojis` predicates for spiking/new/unanswered (fires/absent at boundaries).
- Integration: a poller restart simulation (two pollers, one db) shows a down-time thread
  flagged new and pre-existing threads not re-flagged.
- Hot-path guard: assert `ranked_threads` issues no sqlite queries (mirror-only reads).

### Rollout Plan
- Phases are independently shippable. Phase 1 (Spiking) can land alone. Phase 3 depends on
  Phase 2. Phase 4 ships disabled by default.
- First-ever run with an empty db (and any degraded-mode restart) stamps all backfilled threads
  `now`; the app-start suppressor (M2) hides ✨ for `new_window` after start, so no storm is ever
  shown. Subsequent normal restarts are clean via `INSERT OR IGNORE` regardless.

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| sqlite db unwritable/locked/corrupt | Low | Med | `load()`, `stamp()`, AND `delete()` all trap-and-degrade to in-memory-only + low busy timeout; never raise into the poller worker; ranking unaffected (M1) |
| Long-lived active thread falsely flagged New | Med | **High** | Prune by explicit evicted-key `delete(keys)` (eviction horizon), never a static first-observed age (Blocker B1) |
| First-run / degraded-mode "everything new" storm | High | Low | App-start suppressor: no ✨ within `new_window` of `app_start_at` (M2); covers both the one-time empty-db case and the every-restart degraded case |
| Spiking glyph contradicts score (velocity off) | Med | Low | Phase 1 sets `velocity-weight` *materially* non-zero so glyph and score agree (Q1) |
| Unanswered proxy false positives | High | Med | Off by default; arithmetic + explainable; LLM variant gated behind proof of value |
| Glyph soup (5+ emoji) hurts scanning | Med | Med | Fixed render order; legend tooltip; Unanswered opt-in keeps the resting set small |

## Review Panel Findings (2026-06-27)

Architect (Gemini) and Staff Engineer (Codex) reviewed against the live working tree (v3's
uncommitted changes), not just HEAD. Both succeeded; findings below are the reconciled
synthesis. Raw outputs: `/tmp/review-panel/Uar6ntkK/{arch,staff}.out`. All blockers and
missing-decision items have been folded into the body above.

### Blockers (both reviewers, convergent)

- **B1 - Prune key was wrong (fixed).** The original `DELETE WHERE first_observed < cutoff`
  did not match the app's retention model: eviction keys on `last_activity`
  (`_evict_threads`, poller.py:89), so a long-lived *active* thread (old `first_observed`,
  recent `last_activity`) is still rendered but its observed row would be purged, then
  re-stamped `now` on the next event - falsely flagged ✨ New (and the `not is_zombie` guard
  does not catch it, since it is not a zombie). **Fix folded in:** `ObservedStore.delete(keys)`
  driven by the exact `to_evict` set `_evict_threads` already builds.
- **B2 - `not is_zombie` guard necessary but not sufficient (doc softened).** Zombie state is
  time-bounded (`resurrection_display_hours`), so the guard alone does not close the
  resurrection-after-prune case; it only does so combined with B1. The over-strong "no
  prune-horizon engineering needed" claim is corrected in Phase 3.

### Missing decisions (both, folded in)

- **M1 - Write-path failure handling.** `stamp()`/`delete()` now specified to trap-and-degrade
  on `sqlite3.OperationalError` (never raise into `_process_item`), with a low busy timeout.
- **M2 - Storm suppressor.** A render-time gate (`now - app_start_at >= new_window`) now kills
  both the one-time empty-db storm and the every-restart degraded-mode storm. This is the
  cheap always-on form of the warmup-grace idea that was otherwise deferred to Monday.

### Validated claims

(a) `_fetch_thread` is the single creation chokepoint - **verified** (`_fetch_channel`/
`reconcile` route through it; the socket listener enqueues a fetch, never creates). (c)
hot-path does no DB I/O - **verified**, provided `first_observed_at` is copied onto
`ThreadEntry` and `ObservedStore` is never handed to web rendering. (d) single-connection
async sqlite is safe - **verified** (one event-loop thread, no executor), caveat folded into
the Concurrency note. (b) the New guard - **qualified**, see B2.

### Answers to the four questions

1. **Defaults:** `spiking_threshold=15` confirmed (matches the `spiking (15+)` tier), *with the
   caveat that `velocity-weight` must be materially non-zero* (folded into Phase 1).
   Unanswered params reasonable **as an ops-channel experiment**. `new_window=60` is fine as a
   number, but see Q2 on the mechanism.
2. **New mechanism: split, resolved proxy-first.** Architect favored building the watermark now
   (the continuous daemon means the window misses overnight arrivals); Staff judged proxy-first
   acceptable *provided the copy stops claiming "since I last looked."* Both agree the store is
   the shared substrate, so the watermark stays additive/deferred. **Resolution:** ship the
   proxy as v1, copy corrected (Summary/Goals), validate Monday.
3. **sqlite vs JSONL:** sqlite confirmed the right choice; the justification is re-grounded on
   "targeted indexed DELETE of explicit evicted keys" (post-B1), not the original age query.
4. **Unanswered proxy:** viable **only** as an ops-channel glyph (standard channels' `min-replies:
   3` plus a client-side fetch-boundary drop mean `max_replies: 2` never fires there) - which is
   exactly where a dropped-ball ask matters. Ship opt-in/off-by-default; do not market it as a
   general signal.

## Decisions (2026-06-27, author; panel-reconciled)

1. **Glyphs locked:** ✨ New, ⚡ Spiking, ❓ Unanswered. (`\N{SPARKLES}`,
   `\N{HIGH VOLTAGE SIGN}`, `\N{BLACK QUESTION MARK ORNAMENT}`.)
2. **Defaults:** `spiking_threshold: 15`, `new_window_minutes: 60`, `unanswered_max_replies: 2`,
   `unanswered_min_age_hours: 2`, `unanswered_enabled: false`. Panel-sanity-checked; validate by
   observation. `velocity-weight` must be materially non-zero in the live config.
3. **"New" mechanism: first-observed + window proxy as v1** (panel-reconciled), copy corrected
   to "recently entered the view." Last-viewed watermark (Alternative 5) deferred, additive.
4. **Storm: mitigated now via the M2 app-start suppressor**, not deferred. The Monday call is
   only whether `new_window=60` and the proxy semantics feel right under real traffic.
5. **Ship Phases 1-4 now, preparing for Monday.** Unanswered ships code-complete but disabled by
   default; enabled in the private config for Monday observation.
6. **Prune by evicted-key set, not age (B1); all store paths trap-and-degrade (M1).** Hard
   prerequisites baked into Phase 2.

## Open Questions (remaining, for Monday)
- [ ] Under real traffic: does `new_window=60` feel like "since I last looked," or is the
  last-viewed watermark (Alternative 5) needed after all?
- [ ] Is the Unanswered ops-channel-only scope acceptable, or worth raising `max_replies` to
  reach standard channels (trading false positives)?

## References
- `2026-06-27-prioritization-and-footprint.md` - v3; this doc's parent (Deferred section, Q6).
- `2026-06-21-ranking-and-triage-redesign.md` - v2; emoji state channel origin, zero-miss.
- Code: `web._emojis` (`web.py:113`), `heat.replies_in_window` (`heat.py:38`),
  `poller._fetch_thread` (`poller.py:299`), `poller._evict_threads` (`poller.py:89`),
  `dismiss.DismissStore` (`dismiss.py`), `main._build_app` wiring (`main.py:64`).
- Review panel raw outputs: `/tmp/review-panel/Uar6ntkK/arch.out` (Architect/Gemini),
  `/tmp/review-panel/Uar6ntkK/staff.out` (Staff Engineer/Codex).
