# Design Document: Heat Re-model + Autoresearch Calibration Arena

**Author:** Scott Idler
**Date:** 2026-06-30
**Status:** Implemented
**Review Passes Completed:** 5/5

## North Star (the objective function)

**The dashboard exists to bubble the MOST IMPORTANT threads to the top for Scott to pay
attention to RIGHT NOW.** Threads he is caught up on sink; threads with fresh, important,
*unseen* activity rise. Every scoring decision and every calibration criterion below is
judged against that single sentence. The score is not "how big is this thread" - it is
"how much does this need my eyes in this moment."

## Summary

The heat model currently paints almost every title red and pins hours-old threads at the
top. This re-models the score around working-hours atrophy, adds two new time metrics
(time-alive, time-since-last), reshapes involvement into a drop-then-rebuild curve, and
makes tiering a knob. Crucially, it also builds a **calibration arena** (Karpathy
Autoresearch style): a deterministic simulation of Scott's real board, a binary
desired-output spec, a scoring harness, and a keep/discard tuning loop, so the knobs are
set by measurement rather than guesswork - and frozen as regression tests so the board
cannot silently drift back to all-red.

## Problem Statement

### Background

One scalar, `thread.heat_score`, orders the list. It is computed in exactly one place,
`heat_breakdown` (`heat.py:145-215`), which `compute_heat` wraps
(`heat.py:218-219`). The single-path invariant (`compute_heat == heat_breakdown(...).overall`)
is tested at `test_heat.py:676-712` and is the governing constraint: this re-model changes
the formula *inside* `heat_breakdown` and must never introduce a second one.

```
score = channel_weight × (base + velocity×velocity_weight) × recency × damping   # heat.py:187
base  = message_count×reply_weight + people_term (capped)                          # heat.py:181
recency = max(decay_floor, 1 − hours_since / decay_hours)                          # heat.py:184
```

### Problem

Two verified defects, both calibration/shape problems (NOT regressions from the
heat-metrics-strip work, which is provably score-identical):

1. **Red-everywhere: bounded threshold vs unbounded score.** `base` grows linearly and
   unbounded with `message_count` (`reply_weight` default 2, `config.py:35`) and
   `people_term` (only capped when `people_weight_cap > 0`, which defaults to `0.0` = no
   cap, `config.py:53`), then `channel_weight` multiplies 1x-3x. But `hot_threshold` is a
   fixed absolute `50` (`config.py:40`). A 40-message thread in a 1.2x channel scores ~117,
   more than 2x the red line before any age penalty. Nearly everything clears 50, so nearly
   every title renders `.heat-hot` red (`base.html:15`, via `heat-{{ row.heat_tier }}` at
   `threads.html:40`). Red has stopped carrying information.

2. **Atrophy is too slow and wall-clock, not working-hours.** `recency` is a linear
   wall-clock ramp over `decay_hours=24` (`config.py:37`). A 1pm thread viewed at 8pm (7h)
   still scores `1 − 7/24 = 0.71`. Worse, the clock runs through nights and weekends when
   nobody is working, so a Friday-4pm thread is stone-cold by Monday even though it is the
   freshest unseen thing on the board. (Note: the `decay-half-life-hours` config key name is
   a historical misnomer - the math was always linear, per `config.py:104-105`.)

The observed pathology (Scott's 2026-06-30 ~8pm screenshot): ~20 threads, almost all red,
the sre-it "Sandbox Google Workspace" and data-platform "Philo migration" threads pinned at
the top since ~1pm/2:30pm, hours idle.

### Goals

- Re-rank by the north star: fresh + important + unseen rises; caught-up sinks.
- Atrophy on a **working-hours** clock (6am-6pm PT, Mon-Fri), faster, weekend/night frozen.
- Add **time-alive** and **time-since-last** as first-class metrics (breakdown fields + strip chips).
- Reshape involvement: posting **drops** a thread hard, then it **rebuilds** as unseen messages arrive.
- Make **tiering** and **time-alive weighting** knobs; let calibration choose their values/shape.
- Build a **calibration arena** so knobs are set by measurement, and freeze it as regression tests.
- Every knob adjustable via config (kebab-case).

### Non-Goals

- **Real per-user "last viewed" tracking.** v1 proxies "last looked" with "last posted"
  (the `last_ts` already computed in `involvement_damping`, `heat.py:120`). `observed.py`
  has no user dimension and no view event; true view-tracking is a documented v2.
  **Explicit North-Star gap (panel finding #7):** a thread Scott *reads but does not reply
  to* gets NO involvement drop under the proxy, so it will not sink even though he is caught
  up on it - the "caught-up sinks" promise is only partially met in v1. The rebuild math is
  monotone and provably will not oscillate, so it converges - just on last-posted, not
  last-seen. The natural v2 hook already exists: the `GET /summarize` title-hover
  (`web.py:479`) is a per-thread view event that a `mark_viewed(user, thread, ts)` write to
  an extended `observed.py` could record. Out of scope here, but named so it is a deliberate
  deferral, not an oversight.
- No change to Slack fetch, Socket Mode, grouping, dismiss, or the channel popover.
- No new external dependencies (`zoneinfo` is stdlib on py>=3.12).
- Not a filter/sort-by-emoji view (separate outstanding feature).

## Proposed Solution

### Overview

Five moving parts, all feeding the one formula in `heat_breakdown`:

1. **`worktime.py`** - a pure `business_hours_between(start_ts, end_ts, work_cfg)` helper
   (DST-correct via `zoneinfo`) that counts fractional working hours in the 6am-6pm PT,
   Mon-Fri band, freezing nights and weekends.
2. **Working-hours atrophy** - `recency` becomes an exponential decay over
   *working-hours-since-last-post* with a configurable half-life, replacing the wall-clock
   linear ramp.
3. **Two new metrics** - `time_alive` (first-post -> last-post, working hours) and
   `time_since_last` (last-post -> now, working hours; this is the atrophy input) added to
   `HeatBreakdown` and the strip.
4. **Drop-and-rebuild involvement** - posting drops the score hard; each subsequent unseen
   message rebuilds it toward full, so a thread that moved on without Scott re-surfaces.
5. **Tiering as a knob** - `classify_tier` selects between absolute-normalized thresholds
   and relative top-N/percentile; calibration picks the winner.

Then the **calibration arena** tunes every knob against Scott's real board.

### The score, re-shaped

```
score       = channel_weight × (base_norm + activity) × atrophy × alive_boost × damping   # ONE line in heat_breakdown

volume      = message_count×reply_weight + people_term(capped)
base_norm   = base_cap × volume / (volume + base_k)          # HARD-CEILINGED saturation -> [0, base_cap), monotone
activity    = min(activity_cap, velocity × velocity_weight)  # freshness burst, kept OUTSIDE the volume ceiling
atrophy     = 0.5 ** (work_hours_since_last / atrophy_half_life_work_hours)   # exponential decay, working-hours clock
alive_boost = 1 + alive_weight × f(time_alive) × atrophy      # longevity lift GATED by freshness; f = time_alive/(time_alive+alive_k) ∈ [0,1)
damping     = drop_and_rebuild(self_user_id, replies) ∈ [involved_drop, 1]    # posting drops hard, unseen msgs rebuild toward 1
```

Three deliberate choices, each closing a review-panel finding:

- **`base_norm` has a HARD asymptotic ceiling** (`base_cap`), not merely sub-linear growth.
  A 1000-message stale thread and a 100-message thread both approach `base_cap`, so *volume
  can no longer dominate*. With atrophy then applied, a huge stale thread (`base_cap × 0.06`)
  falls below a small fresh thread (`base_cap × ~1.0` once its own volume is modest but its
  atrophy is high). This is what makes `stale_is_cold` and "small-active beats big-stale"
  actually achievable - a sub-linear-but-unbounded `normalize` could NOT (panel blocker #1).
- **`activity` (velocity) stays OUTSIDE the volume ceiling**, as its own bounded additive
  term. Folding velocity into `volume` would let a big thread saturate on message-count
  alone and wash out the burst signal for short, active threads - exactly the threads the
  North Star wants surfaced (panel must-fix #5). Note: `velocity_weight` **defaults to 0.0**
  (`config.py:54`); the live `~/.config` sets 5.0 - the calibration baselines against the
  live config, not code defaults (see Calibration Arena).
- **`normalize` and `f` are now concrete, bounded, monotone** (hyperbolic saturation with
  knobs `base_cap`/`base_k`/`activity_cap`/`alive_k`), not hand-wave placeholders (panel
  must-fix #6). `alive_boost`'s `× atrophy` gate means a long-lived thread is lifted only
  while fresh; once idle, `atrophy -> 0` collapses it to ~1. `alive_weight = 0` seed =
  time-alive display-only until calibration says otherwise.

- **`base_norm`** addresses the unbounded-base defect. Candidate: sub-linear message
  scaling (e.g. `reply_weight × sqrt(message_count)` or a soft cap) plus the existing
  `people_weight_cap` made effective. The exact normalization is a **knob/choice** the
  calibration decides; the invariant is that a busy-but-stale thread no longer dominates a
  small-but-active one.
- **`atrophy`** is a true half-life over working hours. With `atrophy_half_life_work_hours`
  small (calibration will find it; a starting guess ~3 work-hrs), a thread idle 3 work-hours
  is at 0.5, idle ~12 work-hours (>1 working day) is ~0.06 -> cold. Weekend/overnight
  contributes 0 working hours.
  - **Worked example (the pinned thread):** the 1pm thread at 8pm PT. Working hours
    1pm->8pm = 5 (the 6pm-8pm tail is outside the window), so `atrophy = 0.5^(5/3) ≈ 0.31`
    vs today's wall-clock `1 − 7/24 = 0.71`. It falls to less than half its current recency
    - un-pinning it. Meanwhile a Fri-4pm thread at Mon-9am is also only 5 working hours old
    (Fri 4-6pm = 2, Mon 6-9am = 3; weekend frozen) -> also ~0.31, i.e. treated as 5 work-hrs
    idle, not 65 wall-clock hours. So the aggressive-atrophy criterion (12 work-hrs -> ~0.06
    cold) and the weekend-frozen criterion (5 work-hrs -> ~0.31 warm) do **not** conflict at
    the seed half-life; calibration confirms and tunes.
- **`alive_boost`** is the time-alive interplay: a long-lived AND still-fresh thread (an
  ongoing incident) gets lifted; `gate(atrophy)` ensures a long-lived but idle thread is not
  propped up (atrophy dominates). `alive_weight` is a knob; `gate` can be set to 0 to make
  time-alive display-only, or inverted to penalize long-lived-and-idle ("wrapped up") - the
  calibration explores this.

### Drop-and-rebuild involvement

Refines `involvement_damping` (`heat.py:94-142`). Today `damping = 1 − involved_damping ×
(msg_fade × time_fade)`: strongest right after Scott posts, fading as `messages_after` and
time grow. The reshape:

- **Bigger initial drop.** Right after Scott posts (0 unseen messages), damping applies its
  full `involved_damping` cut (e.g. multiply by ~0.2 - a knob `involved_drop`), so a thread
  Scott just handled falls well down the list.
- **Rebuild on unseen activity.** Each reply *after* Scott's last post (`messages_after`,
  already computed at `heat.py:121`) rebuilds the score toward 1.0 at rate
  `involved_rebuild_per_msg` (knob). So a thread that accrues new messages Scott has not seen
  climbs back - exactly the "moved on without me, re-surface it" behavior. Time also rebuilds
  (a thread quiet since he posted stays down; a thread active since he posted returns).
- v1 reference point = Scott's last post (`last_ts`); the machinery (`replies` with
  `author_id` + `ts`) already exists. v2 would swap in a real last-viewed timestamp.

### Data Model

`HeatBreakdown` (`heat.py:11-30`) gains two fields (both fractional working hours):

```python
@dataclass(frozen=True)
class HeatBreakdown:
    # ... existing 10 fields ...
    time_alive: float        # working hours, first_post -> last_post
    time_since_last: float    # working hours, last_post -> now (atrophy input)
```

`time_alive` sources from `first_seen_ts` / `float(thread_ts)` (the Slack root ts =
first-post epoch, `thread.py:55,72`); `time_since_last` from `last_activity`
(`thread.py:61`). No `ThreadEntry` schema change - both are derivable today.

New config (`WorkWindowConfig`, a nested `_KebabModel`, referenced from `HeatConfig`):

```yaml
heat:
  work-window:
    timezone: America/Los_Angeles
    start-hour: 6            # 06:00
    end-hour: 18             # 18:00
    work-days: [mon, tue, wed, thu, fri]
  atrophy-half-life-work-hours: 3.0
  alive-weight: 0.0          # calibration sets; 0 = display-only to start
  involved-drop: 0.8         # fraction cut right after I post (bigger than today's 0.5)
  involved-rebuild-per-msg: 0.15
  tier-method: absolute      # or "relative"
  tier-hot: 50               # absolute mode thresholds (recalibrated)
  tier-warm: 20
  tier-hot-count: 3          # relative mode: top-N are hot
  tier-warm-count: 10
```

All auto-map kebab<->snake via `_KebabModel` (`config.py:14-19`); a `model_validator`
mirrors the existing `decay-half-life-hours` migration (`config.py:100-112`) for any renamed
keys.

### API Design

```python
# worktime.py (new, single-word module - avoids shadowing stdlib `calendar`)
def business_hours_between(start_ts: float, end_ts: float, work: WorkWindowConfig) -> float:
    """Fractional working hours in [start_ts, end_ts], counting only the daily
    [start_hour, end_hour) window on work_days, in work.timezone. Pure; DST-correct
    via zoneinfo. Nights/weekends contribute 0. Returns 0.0 when end_ts <= start_ts
    (clock skew / a reply timestamped after `now`), never negative."""

# heat.py - classify_tier gains the method switch (still the only tiering path)
def classify_tier(score: float, rank: int, total: int, config: HeatConfig) -> str: ...
```

**Tiering becomes a two-pass, post-sort step (mandatory ordering change).** Today
`classify_tier(score, config)` (`heat.py:393-398`) is a pure absolute-threshold function
called *before* the `sorted(...)` in both `rank_threads` (`heat.py:401-409`) and
`poller.ranked_threads` (`poller.py:99-109`). Relative tiering is impossible in that
position - it needs to know each thread's rank in the *final* order. So the re-model:
1. Pass 1: compute `heat_breakdown(...).overall` for every thread, then `sort` descending.
2. Pass 2: over the sorted list, call `classify_tier(score, rank, total, config)` with the
   post-sort index. **Both call sites** (`rank_threads` and `poller.ranked_threads`) move
   classification after the sort; neither re-implements the formula.

`tier-method` selects:
- **absolute**: `score >= tier_hot` / `>= tier_warm` (recalibrated; now meaningful because
  `base_norm` is ceilinged so scores are bounded).
- **relative (hybrid)**: hot = `rank < tier_hot_count` **AND** `score >= tier_floor`; warm =
  `rank < tier_warm_count AND score >= tier_floor`. The absolute `tier_floor` is what makes
  `stale_is_cold` hold even in relative mode - on a fully-atrophied board, top-N still yields
  **zero** hot because nothing clears the floor (panel blocker #1). Relative counts clamp to
  `min(count, total)` so a small board never errors.

**Edge-case invariants (must hold in code + tests):**
- `business_hours_between` clamps to `>= 0` (a reply ts after `now`, or start>end, yields 0
  work-hours -> `atrophy = 0.5^0 = 1.0`, i.e. treated as maximally fresh, which is correct
  for an after-hours post: no working time has elapsed yet).
- `damping` clamps to `<= 1.0` (the rebuild can restore a thread toward neutral but never
  *boost* it above a not-involved thread) and `>= involved_drop` floor.
- Relative tiering uses `min(tier_hot_count, total)` / `min(tier_warm_count, total)` so a
  board with fewer threads than the counts does not error; `at_most_N_red` still holds
  because `count(hot) <= total <= N` is possible on a tiny board.
- A monologue (root only, no replies) has `time_alive = 0`, `velocity = 0`; `alive_boost`
  and involvement are no-ops. A thread Scott never posted in: `damping = 1.0`.

### The Calibration Arena (Karpathy Autoresearch)

Methodology, from the second-brain vault notes on Karpathy's Autoresearch ("the loop is the
hero, not the model"; "the skill is only as good as the criteria you define"; arena needs
objective scoring + fast/cheap iterations + bounded environment + low failure cost +
traceable actions; cap 5-15 iterations or it over-optimizes; separate the judge from the
optimizer). Four pieces:

**1. Deterministic board fixture** (`tests/calibration/board.py`). The ~20 threads from
Scott's screenshot, transcribed: channel, `message_count` (`Nm`), participant count (`Np`)
+ which participants are VIP ids, involvement flag (👤: sre-it Sandbox, platform-internal,
sre-it Claude-auth, it-helpdesk Codex), heated/zombie flags where shown, and synthesized
first/last-post timestamps against a fixed `now` (~2026-06-30 20:00 PT). The two "pinned"
threads (sre-it Sandbox, data-platform Philo) get last-post ~1pm/2:30pm. Running the
**current** knobs against this fixture must reproduce the pathology (nearly all red, those
two pinned top-2) - that is the baseline the loop improves on.

**2. Binary desired-output spec** (`tests/calibration/criteria.py`). Each a true/false
predicate over the ranked board (Scott approved this draft; N is a knob, start 3-5):

- `at_most_N_red`: count(tier == hot) <= N.
- `lunchtime_threads_demoted`: the two ~1pm/2:30pm idle threads are neither red nor in top-5.
- `active_recent_top3`: a thread with high **recent activity** (several messages in the last
  working hour, i.e. a high `activity`/velocity term) is in the top 3. NOTE: this is
  deliberately *not* phrased as alternating-author "back-and-forth" - `structural_heat`
  (`heat.py:277`) feeds only `is_heated` -> the 🌶️ glyph, NOT the ranking score
  (`compute_heat`), so an alternation signal is not in the score today (panel blocker #3).
  v1 uses the in-score `activity`/velocity signal to satisfy this. Folding `structural_heat`
  into the ranking (as a new knob-weighted term inside `heat_breakdown`, still single-path)
  is a documented option, **default off**, that calibration can enable if velocity alone
  under-serves the criterion.
- `stale_is_cold`: a thread idle > 1 full working day (~12 work-hrs) is tier == cold.
- `weekend_frozen`: a Fri-4pm thread evaluated Mon-9am is still >= warm.
- `involvement_drop_then_rebuild`: a thread Scott just posted in drops below its
  pre-post rank, then a fixture with N unseen replies after his post ranks higher than one
  with 0.
- `vip_lift_capped`: VIP presence lifts but does not run away (people_term respects the cap).

**3. Scoring harness** (`tests/calibration/score.py`). Runs the fixture through
`heat_breakdown(...).overall` + `classify_tier` (never a re-implemented formula - the exact
"Alternative 1" the strip doc rejected, `2026-06-30-heat-metrics-strip.md:309-316`) for a
given `HeatConfig`, returns `(pass_count, failures, soft_distance)`. Pure Python,
milliseconds, no network.

**4. Calibration loop** (`bin/calibrate.py`, a dev tool, not CI). Baseline the current
knobs (report the score - expected: many criteria fail), then perturb one knob, re-score,
keep if it improves, discard if not; iterate <= 10-15. Emit a report of which knobs
**helped**, which **hurt**, which needed to **change** ("maybe some of what we are doing is
hurting, maybe some needs more, maybe something different"). The optimizer is separate from
the criteria (the judge) to avoid bias. Per the arena's "traceable actions" requirement, the
loop writes its full baseline -> keep/discard -> final trace to a report artifact so the
chosen knob values have visible provenance, not a magic constant.

Winning knobs are written into config defaults + `slack-dashboard.example.yml` + Scott's
private `~/.config`. The fixture + criteria freeze into `tests/calibration/test_calibration.py`
under `otto ci` so the board cannot silently regress to all-red.

### Implementation Plan

#### Phase 1: `worktime.py` + `WorkWindowConfig`
**Model:** opus
- New `src/slack_dashboard/worktime.py` with `business_hours_between` (DST-correct via
  `zoneinfo`, pure, float epochs to match `heat_breakdown`'s `now`).
- New `WorkWindowConfig` nested `_KebabModel` in `config.py`, **nested under `HeatConfig`**
  as `heat.work-window` (NOT composed into `AppConfig` - the config shape is pinned here to
  kill the placement ambiguity the panel flagged).
- `WorkWindowConfig` `@model_validator`: reject `end_hour <= start_hour`, empty `work-days`,
  and an unresolvable `timezone`; fail clearly at boot (panel cheap-win #8).
- `business_hours_between` DST discipline: iterate LOCAL calendar dates in `work.timezone`,
  intersect each day's local `[start_hour, end_hour)` window with the span, and convert each
  interval's endpoints to epoch BEFORE subtracting - never subtract aware datetimes across a
  23/25-hour DST day (panel cheap-win #10). 6am-6pm avoids the 2am ambiguous instant, so
  cross-day spans are the only DST risk.
- Full unit tests in isolation: intra-day spans, overnight (frozen), weekend (frozen),
  spring-forward and fall-back DST-boundary spans, multi-day spans. No wiring yet.

#### Phase 2: Re-model the score in `heat_breakdown`
**Model:** opus
- Replace `recency` (`heat.py:184`) with working-hours exponential atrophy via
  `business_hours_between` + `atrophy_half_life_work_hours`.
- Add `base_norm` (HARD-ceilinged saturation, `base_cap`/`base_k`) + `activity` (velocity
  kept OUTSIDE the ceiling, `activity_cap`) + `alive_boost` (freshness-gated `× atrophy`).
- Reshape `involvement_damping` into drop-and-rebuild (`involved_drop` +
  `involved_rebuild_per_msg`), clamped to `[involved_drop, 1.0]`.
- Add `time_alive` / `time_since_last` to `HeatBreakdown`.
- **Move tiering after the sort:** `classify_tier(score, rank, total, config)` with the
  `tier-method` switch (absolute vs relative-hybrid-with-floor); update BOTH `rank_threads`
  and `poller.ranked_threads` to classify in a second pass over the sorted list.
- **All scoring knobs land HERE** (the formula needs them): the `heat.work-window`
  `WorkWindowConfig`, `atrophy_half_life_work_hours`, `base_cap`/`base_k`/`activity_cap`,
  `alive_weight`/`alive_k`, `involved_drop`/`involved_rebuild_per_msg`, `tier_method` +
  `tier_hot`/`tier_warm`/`tier_hot_count`/`tier_warm_count`/`tier_floor`. `model_validator`
  migration mirrors `config.py:100-112`.
- **Extend the DEBUG log** (`heat.py:188-201`) to emit the new factors -
  `atrophy`, `base_norm`, `activity`, `alive_boost`, `time_since_last`, and the selected
  `tier_method` - or the calibration loop is undebuggable (panel cheap-win #9).
- **Preserve the single-path invariant tests** (`test_heat.py:676-712`); update the
  pinned-value tests to the new formula.

#### Phase 3: Strip + docs wiring
**Model:** sonnet
- Document every new knob in `slack-dashboard.example.yml` (config MODEL already landed in
  Phase 2; this phase only documents + defaults, no new fields).
- Add time-alive / time-since-last chips to `_heat_strip` (`web.py:114-161`) + `HeatChip`,
  glyph constants (`web.py:43-48`), CSS (`base.html`), per the strip doc's format/dimming
  conventions.

#### Phase 4: Calibration arena
**Model:** opus
- `tests/calibration/board.py` with **TWO fixtures** (panel over-fit warning): (a) the busy
  screenshot board, (b) a contrast board (a near-idle weekend-morning / mostly-cold board) so
  the loop cannot over-fit a single 20-thread snapshot. `criteria.py` (binary spec),
  `score.py` (harness calling `heat_breakdown` + `classify_tier`).
  - Over-fit-guard scope (honest accounting): most criteria (`lunchtime_threads_demoted`,
    `active_recent_top3`, `involvement_drop_then_rebuild`, `vip_lift_capped`) name threads
    that exist only on the busy board, so they are asserted against the busy fixture. The
    contrast board defends the **board-agnostic invariants**: `weekend_frozen` (Friday-4pm
    thread stays >= warm evaluated Monday), `at_most_N_red` (relative tiering must NOT paint a
    quiet board all-red), and `stale_is_cold`'s property (long-idle threads go cold). The
    frozen regression test re-asserts those three on the contrast fixture; it does not claim
    every criterion is defended on both boards.
- **Baseline against Scott's LIVE `~/.config` knobs, not code defaults** (`velocity_weight`
  5.0, the real channel/people weights, `people_weight_cap` 30) - transcribe them as the
  starting `HeatConfig`. Baselining code defaults (`velocity_weight` 0.0) would tune from the
  wrong starting point (panel doc-claim correction).
- `bin/calibrate.py` loop (perturb -> re-score -> keep/discard, <=15 iters, writes a
  knob-delta trace report).
- Verify the busy-board baseline reproduces the pathology (nearly all red, the two threads
  pinned) under the live knobs.

#### Phase 5: Lock + freeze
**Model:** sonnet
- Write winning knobs into config defaults + example.yml + private config note.
- Freeze board + criteria as `tests/calibration/test_calibration.py` under `otto ci`.
- Update CLAUDE.md chip vocabulary + strip tooltips for the new chips.

## Alternatives Considered

### Alternative 1: Just raise `hot_threshold`
- **Description:** Bump the absolute red line so fewer threads cross it.
- **Pros:** One-line change.
- **Cons:** Does not fix atrophy (stale threads still pinned) or the unbounded-base scaling;
  the threshold drifts out of calibration as channel weights change.
- **Why not chosen:** Treats the symptom, not the model; the north star needs re-ranking.

### Alternative 2: Pure relative tiering, no re-shape
- **Description:** Always mark top-N red, leave the score formula alone.
- **Pros:** Guarantees few reds.
- **Cons:** Ranking itself is still wrong (stale big threads still rank top); relative tiers
  just recolor a bad order. And "hot" is always populated even on a dead board.
- **Why not chosen:** The order is the problem, not only the coloring. Relative tiering is
  offered as a *knob*, not the whole fix.

### Alternative 3: Hand-tune knobs without the arena
- **Description:** Adjust config values by eye until the board looks right.
- **Pros:** No harness to build.
- **Cons:** Not measurable, not reproducible, silently drifts; exactly the situation that
  produced red-everywhere. Karpathy: "the loop is the hero."
- **Why not chosen:** The arena is the deliverable Scott asked for.

## Technical Considerations

### Dependencies
`zoneinfo` (stdlib, py>=3.12, confirmed resolving `America/Los_Angeles` with correct DST).
No new external deps.

### Performance
`business_hours_between` is O(days in span); threads are days old at most, so trivial.
Scoring cost per thread is unchanged order-of-magnitude. The calibration loop is a dev tool,
runs offline.

### Security
None. Display/ranking only, no new inputs or external calls.

### Testing Strategy
- `worktime.py`: isolated unit tests (intra-day, overnight, weekend, DST, multi-day).
- `heat.py`: single-path invariant preserved; per-field breakdown tests updated to the new
  formula; involvement drop-and-rebuild has explicit before/after-post fixtures.
- Calibration: the fixture + binary criteria run under `otto ci` as regression tests.
- `otto ci` green per phase.

### Rollout Plan
Ship via the normal `uv run`/Dockerfile path (now `docker compose`). No migration. The
winning knobs land in the live `~/.config` file; restart the container to apply.

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Re-model changes scores in a way the invariant test can't catch | Med | High | Invariant holds one-formula; the calibration criteria are the real acceptance gate |
| Calibration over-fits the single screenshot board | Med | Med | Cap iterations (5-15); keep criteria general (ratios/ordering, not exact scores); add a 2nd fixture board if needed |
| Business-hours math wrong at DST/edges | Low | Med | Isolated unit tests incl. DST-boundary spans; zoneinfo verified |
| Relative tiering makes "hot" always-populated on a quiet board | Med | Low | `at_most_N_red` allows fewer; hybrid (relative + absolute floor) available as a tier-method |
| "Last posted" proxy misranks vs true "last viewed" | Med | Low | Documented v1 limitation; v2 view-tracking scoped separately |
| Fixture ages synthesized, not real | Med | Med | Pathology reproduces for any recent ages (base alone exceeds threshold); refine from a DB/DEBUG dump if a criterion needs exact timing |

## Resolved Questions

- [x] **Tiering method:** a knob (`tier-method`: absolute-normalized vs relative top-N);
      calibration picks. (Scott: "make tiering a knob, let calibration decide.")
- [x] **Last-viewed:** v1 proxies "looked" with "posted" (`involvement_damping.last_ts`);
      real per-user view-tracking is v2/out-of-scope.
- [x] **Fixture board:** transcribed from Scott's 2026-06-30 screenshot (~20 threads) with
      synthesized ages; the two pinned threads get ~1pm/2:30pm last-posts. Refine from a live
      DB/DEBUG dump only if a criterion needs exact timing.
- [x] **Decay shape:** true exponential half-life over working hours (honestly named
      `atrophy-half-life-work-hours`), replacing the mislabeled linear ramp.
- [x] **Module name:** `worktime.py` (avoids shadowing stdlib `calendar`).

## Open Questions

- [ ] `N` for `at_most_N_red` and the initial `atrophy_half_life_work_hours` are seeded from
      Scott's "use my draft" and will be *set by the calibration*, not pre-decided.

### Review-panel revisions (incorporated)

Both reviewers (Architect + Staff Engineer) converged; the following were folded into this
doc rather than left open: hard base ceiling so `stale_is_cold` is achievable (blocker #1);
`classify_tier` moved after the sort, rank-aware, relative mode carries an absolute floor
(blocker #2); `structural_heat` confirmed glyph-only, criterion reframed to the in-score
`activity` signal (blocker #3); config shape pinned to `heat.work-window`, all knobs added in
Phase 2, phase ordering fixed (blocker #4); velocity kept outside the base ceiling (must-fix
#5); `normalize`/`f` defined as bounded monotone saturations (must-fix #6); last-viewed
North-Star gap stated with the v2 hook named (must-fix #7); config validation + new DEBUG
fields + DST epoch-before-subtract discipline (cheap wins #8-10); second contrast fixture +
live-config baseline (over-fit). Confirmed non-issue: relative tiering does NOT break
grouping/compact-fold (`heat_tier` is CSS-only, fold uses global rank).

## References

- `docs/design/2026-06-30-heat-metrics-strip.md` (+ impl-notes) - the strip this extends;
  single-arithmetic-path constraint (`:49-52, 148, 309-316`).
- `src/slack_dashboard/heat.py:145-215` (`heat_breakdown`), `:94-142` (`involvement_damping`),
  `:184` (recency), `:393-409` (tiering/ranking), `:277-342` (`structural_heat`).
- `src/slack_dashboard/thread.py:14-28` (`ReplyRecord`), `:51-76` (`ThreadEntry`).
- `src/slack_dashboard/config.py:34-112` (`HeatConfig` + migration), `:14-19` (`_KebabModel`).
- `src/slack_dashboard/observed.py:12-20` (store schema; no user/view dimension).
- `src/slack_dashboard/web.py:114-161` (`_heat_strip`), `base.html:15-17` (tier colors).
- Second-brain vault: Karpathy Autoresearch notes (methodology: loop-is-the-hero, criteria
  are everything, arena's five requirements, cap iterations, separate judge/optimizer).
