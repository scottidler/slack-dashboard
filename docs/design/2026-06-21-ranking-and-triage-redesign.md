# Design Document: Ranking & Triage Redesign

**Author:** Scott Idler
**Date:** 2026-06-21
**Status:** Implemented
**Review Passes Completed:** 5/5 + external review (Architect/Gemini, Staff Engineer/Codex)

## Revision Log

- **2026-06-21 r3 — consensus round.** Two pushbacks taken back to the reviewers. (1) *Binary read/dismiss model* (Architect): pushback held — the model is sound to ship and the `status`-discriminator JSONL hook gives zero-migration forward-compat to a future ack/snooze state; no structural change needed. (2) *`min_replies` as definitional* (Staff Engineer): partially conceded — the definitional framing is accepted, but a flat global `3` also drops two-reply threads, so adopted a **per-channel `min_replies` override** (`1` for high-weight ops channels) layered over the global default.
- **2026-06-21 r2 — external review folded in.** Architect (Gemini) and Staff Engineer (Codex) both reviewed against the live code. Resolved blockers: zero-miss scope vs. existing `min_replies`/`threads[:15]` gates (see Goals + "Zero-miss invariant"); resurrection trigger broken by the listener overwriting `last_activity` before fetch (see Resurrection); new ranking fields must merge across all three write paths (see "State merge contract"); `_threads` never evicts dead entries (see Architecture); wildcard channel weights need `fnmatch`; dismiss check must precede `fetch_replies`; `resurrected` flag had no clear mechanism (now computed, not sticky); "live-tunable" softened to config-edit + restart; `/summarize` route kept (no breaking rename).

## Summary

slack-dashboard works mechanically (Socket Mode + REST hybrid, heat ranking, AI summaries) but isn't yet useful enough to replace the habit it was built to replace: skimming every channel. This redesign reframes the product around a single job — **catch what skimming would have caught, so I can stop skimming** — and changes ranking, triage state, and presentation to serve that job. The headline shift: stay clean through **density, not filtering**, so the tool can be trusted to never hide a live thread.

## Problem Statement

### Background

As an IC and early manager, Scott read essentially every message in every channel he was in, within ~5 minutes of posting. That ground-level intel was a real edge. After moving to Director (two teams: SRE and Data Platform, plus two security engineers, plus the channel sprawl that comes with the title), keeping up that way is no longer physically possible. Most Directors respond by going blind to channel-level activity. Scott refuses to.

slack-dashboard v0.3.1 already monitors 22 channels in real time and heat-ranks threads, but Scott tried it and bounced. The ranking didn't reliably elevate the right conversations, and the UI (card-based, verbose) wasn't dense or fast enough to scan. UX is explicitly not Scott's strong suit, and the previous surface grew faster than it could be finished.

### Problem

The dashboard does not yet earn enough trust to displace skimming. Two root causes:

1. **Ranking measures loudness, not relevance.** Heat today is `replies + participants`, decayed by recency. It ignores *which channel* a thread is in (Scott cares far more about `#sre` and `#data-platform` than any `#proj-*`), ignores *velocity* (a thread spiking now vs. one that accreted slowly over a day), and has no concept of a *resurrected* thread.
2. **Presentation forces a filtering/visibility tradeoff.** Card UI is too heavy to show everything, so the instinct is to filter — but any filter that hides a live thread reintroduces exactly the blindness the tool exists to prevent.

### Goals

- Replace channel-skimming for **un-tagged** activity with a single scannable pane.
- **Zero structural misses (scoped):** no *trackable live thread* is ever hidden by the tool's relevance judgment. Ranking may order things wrong (costs time); it may never make a trackable thread invisible (costs blindness). "Trackable" is defined by `min_replies` (a thread = an actual multi-message conversation, not a lone post) — see the Zero-miss invariant below for exactly which gates are definitional vs. forbidden.
- Rank to put the thread Scott most needs near the top, using tunable, *explainable* signals — channel weight, size, velocity, recency.
- Latency target: surfaced **within the hour** of relevant activity (the old bar was ~5 minutes; this is the deliberately relaxed, sustainable replacement).
- Keep the surface small enough to actually finish: compact rows + one interaction.

### Non-Goals

- **Replacing Slack.** This is a triage lens over Slack, not a client. Not yet.
- **Ranking by @-mentions.** Scott reads every thread he is tagged in regardless; Slack already handles that path perfectly. Mention-of-me is therefore explicitly *out of scope as a ranking signal* — the dashboard exists for the threads where he is **not** tagged but should still know.
- **Auto-demoting threads on read.** v1 does not infer "you've seen it." Removal is explicit (see Dismiss). Read-state-aware ranking ("unseen delta") is a deliberately deferred idea, not part of this design.
- **Rich in-row actions.** Inline reply, send-thread-to-AI, copy-to-clipboard are v2. v1 ships compact rows + click-through only.
- **Multi-user / auth.** Single-user, LAN-only deployment is unchanged.
- **Changing the Socket Mode + REST architecture.** That hybrid already works and is out of scope here.

## Proposed Solution

### Overview

Three coordinated changes:

1. **Ranking v2** — multiply heat by a per-channel weight, add a velocity component, and detect resurrected threads. All signals stay simple arithmetic so a wrong ranking is always explainable and hand-tunable via config.
2. **Triage state** — introduce an explicit, **persisted, permanent Dismiss**. Reading a thread does nothing to its rank. Dismissing removes it for good. A long-dead thread that gets new activity comes back as a `:zombie:` at the top.
3. **Compact presentation** — replace cards with dense single-line rows (`channel:title  counts  emoji`), logically grouped, with emoji as a high-density state channel, hover for the AI summary, and click-through straight to the Slack thread. Density is what lets the tool show *everything* live and still be scannable, which is what makes zero-miss compatible with a clean UI.

### Architecture

No change to the Socket Mode listener / REST poller hybrid. Changes are localized:

- `heat.py` — new weighting, velocity, and resurrection logic in `compute_heat` / a new scoring path.
- `thread.py` — `ThreadEntry` gains fields for velocity tracking and resurrection state.
- `poller.py` — maintains a rolling per-thread reply-timestamp window (for velocity), detects resurrection on activity gaps, and consults a dismissed-set so dismissed threads are never re-created. **Also gains an explicit eviction step**: today nothing ever deletes from `self._threads` (it is only written at `poller.py:193`), and `filter_stale_threads` filters at *render* time only, so dead threads accumulate in memory forever. Eviction must `del` entries that are both past `max_thread_age_days` *and* not eligible for resurrection, plus any dismissed key, on each refresh.

**State merge contract.** There are three write paths to a `ThreadEntry`, and every new ranking field must be handled in all three or it will be silently reset:
1. **Socket listener** (`listener.py:75-81`) — increments `reply_count`, bumps `last_activity`; must also append to `reply_timestamps` and capture the resurrection gap.
2. **Incremental fetch** (`poller.py:156-158`) — same in-place updates for new replies.
3. **Full fetch** (`poller.py:178-191`) — rebuilds a fresh `ThreadEntry` preserving only `title`/`summary`/watermarks today; it **must additionally carry forward `reply_timestamps`, `first_seen_ts`, and `resurrection_event_ts`** from the existing entry, or every periodic full refresh wipes velocity and zombie state. This is the highest-risk wiring in the change.
- New `dismiss.py` — a small persisted store (append-only JSONL) of dismissed `(channel_id, thread_ts)` keys, loaded on startup. This is the only state that must survive a restart; everything else is rebuilt from Slack on backfill.
- `web.py` + templates — compact-row partial, grouping, `POST /dismiss/...`, deep-link generation.
- `config.py` — per-channel weights, velocity/resurrection knobs, workspace subdomain for deep links, decay-field rename.

### Data Model

`ThreadEntry` additions:

```python
@dataclass
class ThreadEntry:
    # ... existing fields ...
    reply_timestamps: list[float] = field(default_factory=list)  # rolling window for velocity
    resurrection_event_ts: float = 0.0  # ts of reviving activity; zombie state computed from this
    first_seen_ts: float = 0.0          # thread creation time (from thread_ts), for age/resurrection
```

Velocity is `len(replies within the last velocity_window_minutes) / window`. The `reply_timestamps` list is pruned to the window on each update and capped (oldest dropped past a hard max) so memory stays bounded. Velocity history is intentionally **not** persisted — it is ephemeral and rebuilds naturally.

Dismissed store record (JSONL, one per line):

```json
{"channel_id": "C0123", "thread_ts": "1718900000.000100", "status": "dismissed", "dismissed_at": "2026-06-21T15:04:05Z"}
```

The bare-key set is loaded as `set[tuple[str, str]]`, but each record carries an explicit `status` discriminator (loader defaults missing values to `"dismissed"` for backward compat). This is a near-zero-cost forward-compat hook: a future intermediate state (`"acknowledged"`, `"snoozed"`) slots into the append-only log with no migration. v1 only ever writes `"dismissed"`; the binary model ships unchanged, but the door is left open without committing to it now.

### Heat Formula v2

```
base      = reply_count * reply_weight + participant_count * participant_weight
velocity  = replies_in_window / velocity_window_minutes
recency   = max(decay_floor, 1.0 - hours_since_last_activity / decay_hours)   # linear ramp
score     = channel_weight * (base + velocity * velocity_weight) * recency
```

- `channel_weight` — per-channel multiplier, default `1.0`. e.g. `sre: 2.0`, `data-platform: 2.0`, `proj-*: 0.5`. This is the single highest-leverage new knob.
- `velocity_weight` — how much a *currently spiking* thread is boosted over one that merely accumulated replies slowly.
- **Resurrection:** a thread is resurrected when fresh activity lands after a long quiet gap on an old thread. Two corrections from review make this actually work:
  - **The gap must be captured before any write path overwrites `last_activity`.** The Socket Mode listener (`listener.py:80-81`) bumps `existing.last_activity` the instant an event arrives — *before* it enqueues the fetch. If the poller reads `last_activity` after that, the old value is already gone and resurrection can never trip on live events. Fix: the listener computes `gap = event_time - existing.last_activity` at that boundary and carries the resurrection decision (or the prior timestamp) on the `FetchItem`. The incremental/full fetch paths do the same capture before their in-place update.
  - **`resurrected` is computed, not sticky.** Rather than a boolean that something must later flip back to `False` (there is no such tick), the zombie state is derived at rank time: a thread shows `:zombie:` while `now - resurrection_event_ts < resurrection_display_hours` and `first_seen_ts` is older than `resurrection_age_days`. We store `resurrection_event_ts` (the timestamp of the reviving activity); the marker ages out on its own with no clearing pass.

  Because `recency` snaps back to ~1.0 on fresh activity and the thread is already large, a resurrected thread naturally rises to the top; the computed flag only drives the `:zombie:` glyph.

Ranking remains a total ordering over **all non-dismissed, non-dead** threads. "Dead" (aged past `max_thread_age_days` with no activity) is the *only* automatic removal; it is reversible by resurrection. Nothing is hidden for being "unimportant."

**Dismiss outranks resurrection.** Dismiss is permanent and checked on insert, so a dismissed thread that later receives new activity stays gone — it does **not** zombie back. The two removal concepts are distinct: *dead* is automatic and reversible (resurrection); *dismissed* is manual and final.

### Zero-miss invariant (exactly which gates are allowed)

The headline promise only holds if every silent-hide path in the current code is accounted for. Audited against the live code, there are two:

- **`threads[:15]` render cap (`threads.html:2`) — FORBIDDEN, must be removed.** A fixed row cap is a relevance-judgment hide: thread #16 vanishes purely for being ranked 16th. Density exists precisely so we can render *all* trackable threads; the compact-row redesign removes this cap. If the list is ever genuinely too long to scroll, that is a grouping/collapse problem, never a truncation.
- **`min_replies` source filter (`client.py:103`, applied via `poller.py:214`) — ALLOWED, definitional, but made per-channel.** This is not a relevance hide; it is the *definition* of "a thread." A post with too few replies is a lone message, not a conversation, and per the non-goals Scott reads everything he is tagged in anyway. `min_replies` therefore scopes what counts as trackable; it does not hide trackable threads. **However**, a flat global `3` also drops *two*-reply threads, which in a high-stakes channel can be exactly the untagged intel the dashboard exists to catch (e.g. `#incidents`: parent "seeing auth failures from prod" + "same in us-east" + "tied to deploy X" — three messages, two replies, no mention). So: keep the global default at `3`, but support a **per-channel `min_replies` override** set to `1` for high-weight operational channels (`#incidents`, `#ask-security`, `#sre`, `#data-platform`). The same watchlist that earns a `channel_weight` of `2.0` earns `min_replies: 1`. With that, the scoped zero-miss promise holds: nothing trackable-by-its-own-channel's-definition is hidden.

No other gate (heat threshold, tier filter, "hot only" view) may be introduced — those would re-break the invariant.

### Emoji State Channel

Emoji encode thread state at one glyph per signal — the densest possible encoding for a compact row:

| Emoji | Meaning | v1? |
|-------|---------|-----|
| `:zombie:` | Resurrected — long-dead thread with fresh activity, floated to top | yes |
| `:fire:` | Hot tier / high velocity | yes |
| `:rotating_light:` | Incident/urgency keyword detected | v2 |
| `:speech_balloon:` | A VIP (boss, direct report) is participating | v2 |

### API Design

```
GET  /                                     # shell page (HTMX)
GET  /threads?group-by={channel|size|velocity|participants}
                                           # compact, grouped, ranked rows; default group-by=channel
                                           # renders ALL trackable threads — no [:15] cap (see Zero-miss invariant)
POST /dismiss/{channel_id}/{thread_ts}     # permanent dismiss; HTMX removes the row
GET  /summarize/{channel_id}/{thread_ts}   # AI summary for hover panel (existing route kept, NOT renamed)
GET  /health
```

> Note: the existing `/summarize/{channel_id}/{thread_ts:path}` route is kept as-is. An earlier draft renamed it to `/summary`, which would have orphaned `threads.html:29`, `summary.html:5`, and `test_web.py` for no benefit. No rename.

- **Click a row** → anchor to the Slack deep link for the thread. Web form:
  `https://{workspace}.slack.com/archives/{channel_id}/p{thread_ts_without_dot}`
  (`workspace` from config, e.g. `tatari`). Click navigates only — it does **not** mark read or dismiss in v1.
- **Hover a row** → panel showing the AI summary and (v2) derived tags. Summaries are already generated fire-and-forget by the poller; hover just reveals them.
- **Dismiss** → small control on the row; one click, permanent.

### Implementation Plan

#### Phase 1: Config + data-model scaffolding
**Model:** sonnet
- Add `channel_weights: dict[str, float]`, `velocity_weight`, `velocity_window_minutes`, `resurrection_gap_hours`, `resurrection_age_days`, `resurrection_display_hours`, and `workspace` to config (kebab-case, defaults preserve current behavior — all weights `1.0`, velocity contribution `0`).
- **Channel weights support glob patterns** (e.g. `proj-*: 0.5`). Lookup is not a plain `dict.get()` — resolve a channel's weight by testing keys with `fnmatch` (exact keys win over globs; default `1.0` if none match). The doc previously implied a flat dict, which silently would not match `proj-*`.
- **Per-channel `min_replies` override.** Add `channel_min_replies: dict[str, int]` (glob-aware, same resolution as weights) layered over the global `fetch.min_replies` default of `3`. The source filter currently applies one global value (`poller.py:214` → `client.py:103`); change the poller to pass the *resolved per-channel* `min_replies` per fetch. High-weight ops channels get `1`. (Consensus outcome with the Staff Engineer: a flat `3` silently drops two-reply threads, which is exactly the untagged incident/security intel the tool must catch.)
- Rename `decay_half_life_hours` → `decay_hours` (the math is a linear ramp, not a half-life) and add `decay_floor` defaulting to `0.01` (currently hardcoded). With `decay_hours=24` and `decay_floor=0.01` the formula is byte-for-byte the current behavior. Keep an alias for backward-compatible config loads.
- Add `reply_timestamps`, `resurrected`, `first_seen_ts` to `ThreadEntry`.

#### Phase 2: Heat formula v2
**Model:** opus
- Implement channel weighting, velocity, and resurrection detection in `heat.py` / `poller.py`.
- Populate and prune `reply_timestamps`; set `first_seen_ts` from `thread_ts`; record `resurrection_event_ts` when the gap trips. **The gap must be captured in the Socket listener (`listener.py:80-81`), not just the poller** — the listener overwrites `last_activity` before the fetch is enqueued, so the prior timestamp must be read there and carried on the `FetchItem`. The incremental/full fetch paths capture before their own in-place update. See the State merge contract — all three paths touch these fields.
- Zombie state is computed at rank time from `resurrection_event_ts` + `first_seen_ts`; there is no flag-clearing pass.
- Unit tests: channel weight ordering, velocity boost, resurrection trip/clear, decay rename equivalence.

#### Phase 3: Dismiss persistence
**Model:** sonnet
- New `dismiss.py`: load JSONL on startup into a `set[tuple[str, str]]`, append on dismiss. **Append atomically** (open in append mode + flush/fsync, or write-temp-then-rename for rewrites) so a crash mid-write can't corrupt the permanent record.
- Poller consults the set **before calling `fetch_replies`**, not only at insert — checking at insert still burns a REST/rate-limit call to fetch a thread we will immediately discard. Dismissed keys short-circuit the fetch entirely.
- **On dismiss, also evict the live entry from `_threads`** — the thread already exists in memory from before the dismiss, so `ranked_threads()` must additionally filter against the dismissed set (belt-and-suspenders) so it can never reappear on the next render or refresh.
- `POST /dismiss/...` wired to the store; HTMX row removal.

#### Phase 4: Compact-row UI
**Model:** sonnet
- Replace card partial with a single-line row partial: `channel:title  <replies> <participants>  <emoji>`.
- Grouping (`group-by` param), emoji rendering, hover summary panel, click-through deep links.
- Keep it deliberately minimal; defer all rich actions to v2.

#### Phase 5: Tests, docs, tuning pass
**Model:** sonnet
- End-to-end render test, dismiss persistence across restart, deep-link format test.
- Update README and example config with the new knobs and sane starting weights.

## Alternatives Considered

### Alternative 1: Filter to "hot" threads only
- **Description:** Show only threads above a heat threshold.
- **Pros:** Trivially clean UI.
- **Cons:** Any hidden live thread is a potential blindness miss.
- **Why not chosen:** Directly violates the zero-miss goal. Density solves cleanliness without hiding anything.

### Alternative 2: Demote-on-read (unseen-delta ranking)
- **Description:** Track a per-thread "seen" watermark; rank by *unseen* activity so read threads sink and resurface on new replies.
- **Pros:** Elegant; the list self-cleans without manual dismiss; click does triple duty (navigate + mark read + training signal).
- **Cons:** Infers intent from a click, which is often wrong (you click to glance, not to "finish"); harder to reason about; more state.
- **Why not chosen:** Scott wants explicit control for v1, not inferred read-state. Strong candidate for a future iteration; recorded here so it isn't relitigated from scratch.

### Alternative 3: ML / embedding-based importance ranking
- **Description:** Learn relevance from behavior or embed-and-score threads.
- **Pros:** Could capture relevance that arithmetic misses.
- **Cons:** Opaque, can't hand-tune, can't explain a miss — fatal for a tool whose entire value rests on *trust*.
- **Why not chosen:** Explainability is a hard requirement. Simple weighted signals can be reasoned about and tuned knob-by-knob.

### Alternative 4: Use Slack-native features (sections, stars, keyword alerts)
- **Description:** Lean on Slack's own organization instead of a separate tool.
- **Pros:** No code to maintain.
- **Cons:** No cross-channel heat-ranked single pane; keyword alerts are per-keyword and noisy; sections are manual.
- **Why not chosen:** None of these produce the ranked, dense, cross-channel triage view that is the whole point.

## Technical Considerations

### Dependencies

No new runtime dependencies. JSONL dismiss store uses the stdlib. (`taskstore` was considered for persistence but is overkill for a single append-only set; noted as an option if state grows.)

### Performance

- Velocity adds an O(replies-in-window) prune per update; bounded by a hard cap on `reply_timestamps`.
- Ranking is unchanged in complexity (sort over live threads).
- Dismiss-set lookup is O(1).

### Security

Unchanged from current design: single-user, LAN-only, no auth, secrets via env-var interpolation. As acting Head of Security, Scott is the sole consumer; the dismiss list and summaries contain Slack-derived content and live only on the local box.

### Testing Strategy

Extend the existing 14-module suite: ranking ordering with channel weights, velocity boost, resurrection trip/clear, dismiss persistence across a simulated restart, deep-link URL format, and a render test of the compact row + grouping.

### Rollout Plan

Local-only; ship behind the existing config. All new knobs default to current behavior (weights `1.0`, velocity `0`), so the first run after upgrade ranks identically to today. Tuning weights/velocity currently requires a **config edit + restart** — config is loaded once at `main.py:104` and there is no reload path. Calling this "live-tunable" would overstate it; a hot-reload (file watch or `POST /reload`) is an explicit open question below, not part of v1.

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Channel weighting buries cross-team intel if mis-tuned | Med | High | Defaults are neutral (`1.0`); weights are live-tunable; zero-miss means buried ≠ hidden |
| Permanent dismiss removes something later relevant | Med | Med | Explicit single-user action; "for now" decision; revisit with undo if it bites |
| Resurrection false-positives (periodic low chatter) | Med | Low | `resurrection_gap_hours` + `resurrection_age_days` thresholds; tunable |
| In-memory state lost on restart | High | Low | Backfill rebuilds threads from Slack; only the dismiss set is persisted |
| New ranking fields silently reset by the full-fetch rebuild path | Med | High | State merge contract: all 3 write paths carry forward `reply_timestamps`/`first_seen_ts`/`resurrection_event_ts`; regression test asserts a full refresh preserves them |
| `_threads` grows unbounded (no eviction today) | Med | Med | Explicit eviction of dead, non-resurrectable, and dismissed keys on each refresh |
| UI scope creep repeats the previous abandonment | Med | High | v1 hard-capped at compact rows + click-through; all rich actions are v2 |
| Velocity reply-timestamp window grows memory | Low | Low | Window-pruned and hard-capped per thread |

## Open Questions

- [ ] Slack deep link: web URL (`https://{workspace}.slack.com/archives/...`) vs. `slack://` app protocol — which lands Scott in the right place fastest from both desk and laptop?
- [ ] Default `group-by` — channel, or size/velocity?
- [ ] Decay shape — keep the linear ramp (just renamed) or switch to true exponential? Renaming is the low-risk default for this pass.
- [ ] `velocity_window_minutes` starting value (15? 30?) and `velocity_weight` relative to `reply_weight`.
- [ ] Should dismiss ever be undoable, or is permanent genuinely fine long-term? (Atomic append makes an undo/tombstone cheap to add later.)
- [x] ~~Is `min_replies=3` the right floor for "trackable"?~~ **Resolved (Staff Engineer consensus):** global default `3`, per-channel override to `1` for high-weight ops channels. Remaining sub-question: exact watchlist membership and whether any channel warrants `0`.
- [ ] Config hot-reload (file watch or `POST /reload`) vs. accept restart-to-retune for v1.
- [ ] Observability: any need to log/expose *why* a thread ranks where it does (per-signal contribution), to debug a mis-rank during tuning?

## References

- `docs/design/2026-03-24-slack-dashboard-design.md` — original design
- `docs/design/2026-03-25-socket-mode-redesign.md` — Socket Mode + REST hybrid
- `docs/design/2026-03-25-api-throughput.md` — dual-semaphore fetch throughput
- Current code: `src/slack_dashboard/heat.py`, `thread.py`, `poller.py`, `web.py`, `config.py`
