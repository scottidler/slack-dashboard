# Design Document: Heat-Metrics Strip in the Thread Hover Popup

**Author:** Scott Idler
**Date:** 2026-06-30
**Status:** Implemented
**Review Passes Completed:** 5/5

## Summary

When you hover a thread title, the popup today shows only the quoted first message and
the bullet summary. This adds a compact one-line strip across the top of that popup: the
overall heat score (🌡️) on the left, followed by the factors that compose it, expanding
left to right. It turns the single opaque ranking number into a legible breakdown so you
can see *why* a thread sits where it does, without leaving the dense resting view.

## Problem Statement

### Background

One scalar, `thread.heat_score`, orders the entire list. It is the product of six factors
(`heat.py:108-134`):

```
score = channel_weight × ( base + velocity × velocity_weight ) × recency × damping
base  = message_count × reply_weight  +  Σ people_weight(participant)   (capped)
```

`compute_heat` returns only the final float. The six intermediates (`channel_weight`,
`base`, `velocity`, `recency`, `damping`, and the `people_term`/`message_count` that make
up `base`) exist only as locals and are emitted at DEBUG (`heat.py:123-133`). Nothing in
the UI surfaces them. The hover popup (`GET /summarize`, `web.py:381-414`) builds a
`detail = {"quote", "author"}` dict and renders `partials/summary.html` (quote + bullets);
it never touches the heat model.

### Problem

The ranking is a black box at the point of use. A thread is at rank 3 and you cannot tell
whether that is channel importance, raw volume, a fast back-and-forth, or simply recency.
The information needed to answer that already exists at rank time but is thrown away. To
diagnose or trust the ranking today you must drop to a DEBUG log and grep for the thread.

### Goals

- Show the overall heat score plus its composing factors in the hover popup, as a single
  dense horizontal strip across the top, expanding left to right.
- Each factor is a chip: an emoji plus the value as it enters the formula.
- Reuse the row's existing glyph vocabulary where factors overlap (⚡ velocity, 👤
  involvement, 👑 VIP) so the popup and the row speak one language.
- Guarantee the strip's numbers and the score that ranked the row come from **one**
  arithmetic path. No second formula that can drift.
- Bold the quoted first-message text in the popup (the thread's real "title") so it reads
  as the heading of the detail panel; leave the author attribution at normal weight.

### Non-Goals

- No new ranking behavior. This is display-only; `compute_heat`'s result is unchanged.
- No filter/sort-by-emoji view (that is the separate outstanding feature).
- No change to the row itself, to grouping, or to the channel popover.
- Not surfacing the strip anywhere but the title-hover popup.

## Proposed Solution

### Overview

Introduce a `HeatBreakdown` dataclass and a `heat_breakdown(thread, config, self_user_id,
now)` function in `heat.py` that computes all six factors and the overall score in one
place. Refactor `compute_heat` to return `heat_breakdown(...).overall`, so there is exactly
one arithmetic path. The `/summarize` handler calls `heat_breakdown` once, passes the
result into the `detail` dict, and `summary.html` renders a `.heat-strip` block atop
`.summary-content`.

### The strip

The popup, with the strip prepended above the existing quote and bullets:

```
┌──────────────────────────────────────────────────────┐
│ 🌡️117  🏷️×1.20  📊40m·8p👑  ⚡0.3  ⏱️0.90  👤×0.76      │  <- new strip
├──────────────────────────────────────────────────────┤
│ "Can we get eyes on the deploy?"          - @alice     │  <- .thread-quote (msg now bold)
│ • deploy blocked on a migration                        │  <- existing .summary-bullets
│ • two people disagree on rollback                      │
└──────────────────────────────────────────────────────┘
```

Overall heat leftmost, then 5 chips (message_count and people_term merge into one `base`
chip).

| Chip | Glyph | Source | Face value | Tooltip |
|------|-------|--------|------------|---------|
| Overall | 🌡️ THERMOMETER (new) | `overall` | integer score | `heat score` |
| Channel weight | 🏷️ LABEL (new) | `channel_weight` | `×N.NN` | channel name + weight |
| Base | 📊 BAR CHART (new) | `message_count`, `people_count`, `has_vip` | `Nm·Np` (counts), 👑 appended when a VIP-weighted participant is present | computed `base` value and `people_term` |
| Velocity | ⚡ `_SPIKING` (reused) | `velocity` | raw replies/min, `0.N` | `replies/min in window` |
| Recency | ⏱️ STOPWATCH (new) | `recency` | `0.NN` (0..1) | hours since last activity |
| Damping | 👤 `_INVOLVED` (reused) | `damping` | `×N.NN` (0..1) | involvement damping |

Resting layout is fixed: overall + 5 chips always render, so the strip does not reflow on
hover and chip positions are predictable. A chip that is a no-op for the score is dimmed
(reduced opacity) so the eye skips it while the slot stays put. Three cases are dimmed:

- `channel_weight == 1.00` (multiplier has no effect)
- `damping == 1.00` (not involved, or feature off)
- `velocity_weight == 0.0` (the default, `config.py:54`): the ⚡ chip contributes **nothing**
  to the score even when `vel` is nonzero. Dimming it is the honest signal that the chip is
  reporting an activity rate, not a score contribution, so a nonzero ⚡ beside an unchanged
  🌡️ does not read as a contradiction. The chip's tooltip states the contribution is
  `vel × velocity_weight`. (Review-panel must-fix: velocity honesty.)

Format strings (so values align under `tabular-nums` and the implementer does not guess):

| Chip | Format | Example |
|------|--------|---------|
| 🌡️ overall | `{:.0f}` (integer) | `117` |
| 🏷️ channel_weight | `×{:.2f}` | `×1.20` |
| 📊 base | `{count}m·{count}p` + `👑` if `has_vip` | `40m·8p👑` |
| ⚡ velocity | `{:.1f}` | `0.3` |
| ⏱️ recency | `{:.2f}` | `0.90` |
| 👤 damping | `×{:.2f}` | `×0.76` |

### Why these specific values

- The **🏷️ channel_weight** and **👤 damping** chips are multipliers; they show `×N.NN`
  because that is exactly how they enter the formula.
- The **📊 base** chip shows the message and people *counts* (`Nm·Np`, mirroring the row's
  `count-replies`/`count-people`), not the computed `base` float or the weighted/capped
  `people_term`. Counts are what a human scans; the precise `base` and `people_term`
  numbers live in the native tooltip. 👑 is appended when `_has_vip` is true, signaling
  that person-weighting (not just headcount) is lifting the term.
- The **⚡ velocity** chip shows the raw per-minute `vel` (`heat.py:119`), the same
  quantity ⚡ already means on the row. Note `velocity_weight` defaults to `0.0`
  (`config.py:54`), so velocity contributes nothing to the score by default; the chip
  still reports the activity rate (intrinsic, not contribution). This is documented so a
  nonzero ⚡ next to an unchanged score is not read as a bug.
- The **⏱️ recency** chip shows the decay multiplier in `[decay_floor, 1.0]`.

### Architecture

```
heat.heat_breakdown(thread, config, self_user_id, now) -> HeatBreakdown   [single math path]
        ▲                                  │
        │ .overall                         │ (full struct)
heat.compute_heat(...) -> float            │
                                           ▼
web.summarize route ── detail["heat"] ──> partials/summary.html (.heat-strip)
```

`heat_breakdown` is the single source of truth. `compute_heat` becomes a thin wrapper
(`return heat_breakdown(thread, config, self_user_id, now).overall`) so `rank_threads`
(`heat.py:316-324`) and the poller (`poller.py:99-109`) are unaffected in behavior.

### Data Model

```python
@dataclass(frozen=True)
class HeatBreakdown:
    overall: float          # the ranking score (== compute_heat result)
    channel_weight: float   # multiplier
    base: float             # message_count*reply_weight + people_term (capped)
    message_count: int      # for the Nm face value
    people_count: int       # len(participants), for the Np face value
    people_term: float      # weighted, capped sum (tooltip / precision)
    has_vip: bool           # any participant above default weight -> append 👑
    velocity: float         # raw replies/min in window
    recency: float          # decay multiplier in [decay_floor, 1.0]
    damping: float          # involvement-damping multiplier in [1-involved_damping, 1.0]
```

`has_vip` is computed from a single shared helper `is_vip(thread, config: HeatConfig) ->
bool` (new, in `heat.py`), defined once as "any participant whose `resolve_person_weight`
exceeds `participant_weight`". `heat_breakdown` calls it, and the existing `web._has_vip`
(`web.py:116-119`, which today re-implements the same rule against `AppConfig`) is
refactored in Phase 1 to delegate to `is_vip(thread, config.heat)`. There is then one VIP
rule, not two - the same single-source discipline the breakdown applies to the score.

**👑 semantics (explicit):** the crown means "a VIP-weighted participant is **present** in
this thread", i.e. `is_vip` membership. It does **not** assert "the VIP lifted the score":
`people_weight_cap` (`heat.py:115-116`) can clamp `people_term` so a present VIP contributes
nothing extra, yet the crown still shows. This is the intended reading (the crown flags
*who is here*, mirroring the row's 👑); the 📊 tooltip shows the actual capped `people_term`
so the score effect is still legible. (Review-panel cheap-win: dedup + crown/cap clarity.)

### API Design

```python
def heat_breakdown(
    thread: ThreadEntry,
    config: HeatConfig,
    self_user_id: str | None = None,
    now: float | None = None,
) -> HeatBreakdown: ...

def compute_heat(
    thread: ThreadEntry,
    config: HeatConfig,
    self_user_id: str | None = None,
) -> float:
    return heat_breakdown(thread, config, self_user_id).overall
```

`now` is a float Unix timestamp and defaults to `datetime.now(UTC).timestamp()` (matching
`velocity`/`involvement_damping`/`structural_heat`, which already take a float `now`). So
`recency` inside `heat_breakdown` derives `hours_since = (now - thread.last_activity
.timestamp()) / 3600` (the `structural_heat` pattern at `heat.py:236`), not the datetime
subtraction `compute_heat` does today at `heat.py:110`. Existing callers pass nothing and
get the default; the route passes its request-captured `now` (`web.py:356`).

Route change (`web.py`, the `/summarize` handler) - the breakdown is computed **right after
the `entry is None` guard**, so every response that has a valid thread carries it:

```python
entry = poller.threads.get(key)
if entry is None:                       # web.py:386-390 - no thread, no strip
    return ...error...
now = datetime.now(UTC).timestamp()
breakdown = heat_breakdown(entry, config.heat, poller.self_user_id, now)
detail = {"quote": ..., "author": ..., "heat": breakdown}
# cached branch (web.py:394-397):       render summary.html with {**detail}
# fresh LLM success (web.py:412-414):   render summary.html with {**detail}
# fresh LLM FAILURE (web.py:403-408):   render summary.html with {"error": True, **detail}
```

The route has **three** thread-bearing response paths, not two: the cached branch, the
fresh-success branch, and the fresh **LLM-failure** branch (`web.py:403-408`). The strip's
heat data depends only on `entry`, not on the summary, so it is valid in all three -
including LLM failure, where showing the heat breakdown above the "Failed to generate
summary / Retry" message is strictly better than a bare error. Only the missing-thread
branch (`entry is None`, `web.py:386-390`) omits the strip. `summary.html` therefore renders
the strip in both its non-error and error layouts whenever `heat` is in context. (This
corrects the earlier "error branch has no thread" claim, which was true only for the
missing-thread branch; the LLM-failure branch does have a valid `entry` - review-panel
must-fix.)

### Staleness / freshness

The strip recomputes the breakdown at hover time with a fresh `now`. The row's stored
`heat_score`/`heat_tier` are a snapshot from the last `ranked_threads()` poll (every 30s,
`poller.py:107-108`). The strip's 🌡️ `overall` can therefore differ from the score that
last ranked the visible row along **two** axes, not just one:

1. **Time-decay:** `recency` and `damping` are time-dependent, so they move continuously
   between polls. Drift here is negligible (30s against a 24h decay curve).
2. **Counts/base:** the poller mutates `ThreadEntry` in place (`message_count`,
   `participants`, hence `base`) as new events arrive (`poller.py:263-296, 449`), and the
   hover fetch is `mouseenter once` (`threads.html`). So between the row's last poll and the
   hover, the underlying counts can change and the strip's 📊/🌡️ reflect the **newer** state.

Both are intentional and correct: the strip shows the breakdown *as of now*, which is more
current than the row's last poll, not stale. The alternative (freezing a breakdown onto
`ThreadEntry` at rank time) trades this for a stored field with its own staleness and is
rejected (see Alternatives).

### Implementation Plan

#### Phase 1: `HeatBreakdown` + `heat_breakdown()` refactor
**Model:** opus
- Add the `HeatBreakdown` frozen dataclass to `heat.py`.
- Add the shared `is_vip(thread, config: HeatConfig) -> bool` helper to `heat.py`, and
  refactor `web._has_vip` (`web.py:116-119`) to delegate to `is_vip(thread, config.heat)`
  so the VIP rule exists in exactly one place.
- Add `heat_breakdown(thread, config, self_user_id, now)` containing the math currently
  inline in `compute_heat` (`heat.py:114-122`), plus `message_count`/`people_count`/
  `has_vip` (via `is_vip`). `recency` derives `hours_since` from the float `now`
  (`(now - thread.last_activity.timestamp()) / 3600`, the `structural_heat` pattern at
  `heat.py:236`), not the datetime subtraction `compute_heat` does today at `heat.py:110`;
  this assumes a tz-aware `last_activity` (the poller creates it aware, but `ThreadEntry`
  does not enforce it - note the assumption). Move the existing DEBUG log
  (`heat.py:123-133`) into it, extended to log the added fields, with a DEBUG entry log per
  the logging rule.
- Refactor `compute_heat` to `return heat_breakdown(...).overall`.
- Tests in `test_heat.py`: each field equals the hand-computed factor for known fixtures
  (`_make_thread`, `HeatConfig()`); the single-path invariant
  `compute_heat(t, c) == heat_breakdown(t, c).overall`; and a fixture asserting
  `compute_heat` is numerically unchanged across the refactor. All existing `compute_heat`
  assertions must still pass.
- No UI change in this phase.

#### Phase 2: Wire the breakdown into `/summarize` and render the strip
**Model:** sonnet
- Add new glyph constants in `web.py:26-32` using the `\N{...}` form: `_THERMO`
  (THERMOMETER), `_CHANNEL_WEIGHT` (LABEL), `_BASE` (BAR CHART), `_RECENCY` (STOPWATCH).
  Reuse `_SPIKING`, `_INVOLVED`, `_VIP`.
- In the `/summarize` handler, capture `now`, call `heat_breakdown`, add it to `detail`.
- Render `.heat-strip` at the top of `.summary-content` in `summary.html` (above
  `.thread-quote`), one chip per factor, with native `title=` tooltips mirroring the
  `.count-replies` pattern (`threads.html:21`). Emit no em dashes.
- Bold the quoted first-message text in the popup: the `.thread-quote` message body renders
  bold, while the `.thread-quote-author` `<cite>` (the `- @author` line) stays normal
  weight. Do this in the inline CSS (`.thread-quote { font-weight: 600 }` with the cite
  reset to normal), not by wrapping the text in markup, so attribution does not inherit the
  weight.
- Add `.heat-strip` / `.heat-chip` / dimmed-no-op CSS to the inline `<style>` in
  `base.html` (`base.html:9-97`), matching `.row-counts` (`tabular-nums`, terse).
- Tests in `test_web.py` (assert-substring pattern, as in `test_web.py:123-128`):
  `/summarize` renders the strip glyphs and values; the **missing-thread** branch renders no
  strip; the **LLM-failure** branch (mock the provider to fail) renders the strip *and* the
  error/Retry block; the ⚡ chip carries the dimmed class when `velocity_weight == 0.0`.

#### Phase 3: Polish and convention
**Model:** sonnet
- Confirm the route's `now` flows into `heat_breakdown` consistently with `web.py:356`.
- Tune chip spacing/opacity for the dimmed no-op multipliers; verify `tabular-nums`
  keeps values aligned.
- Document the chip vocabulary (glyph -> factor) in `CLAUDE.md` under the density section.
- Likely small; may fold into Phase 2.

## Alternatives Considered

### Alternative 1: Recompute each factor independently in the route/template
- **Description:** Call `velocity()`, `resolve_channel_weight()`, `involvement_damping()`,
  etc. directly from the route and assemble the display values there.
- **Pros:** No change to `heat.py`; uses existing helpers.
- **Cons:** Two arithmetic paths. `base`, the `velocity_weight` combination, the
  `people_weight_cap`, and the factor ordering would be re-implemented in the route and
  could silently drift from `compute_heat`. Exactly the bug class we must avoid.
- **Why not chosen:** Violates the single-source-of-truth goal.

### Alternative 2: Store the breakdown on `ThreadEntry` at rank time
- **Description:** Have `rank_threads`/the poller stash a `HeatBreakdown` on each thread so
  the strip reads the exact values that ranked the row.
- **Pros:** Strip 🌡️ matches the row's last-polled score exactly.
- **Cons:** Adds a stored field that is stale between polls, must be kept in sync on the
  incremental-update path (`poller.py:449`), and freezes time-dependent factors to the
  poll instant rather than the hover instant. More state, more staleness.
- **Why not chosen:** Recompute-at-hover is simpler and more current; the tiny divergence
  is a feature (live), not a defect.

### Alternative 3: Six chips, no merge
- **Description:** message_count and people_term as separate chips.
- **Pros:** Most literal decomposition.
- **Cons:** Wider strip; the two are mathematically one additive term (`base`). User chose
  the merged form.
- **Why not chosen:** Decided against in requirements.

## Technical Considerations

### Dependencies

No new external dependencies. Internal: `heat.py` already imports `resolve_channel_weight`
and `resolve_person_weight` from `config.py`; `web.py` already imports from `heat`.

### Performance

`heat_breakdown` does the same arithmetic `compute_heat` already did, so ranking cost is
unchanged. The route adds one breakdown computation per hover (already cheap; same cost as
one `compute_heat`). No new I/O.

### Security

None. Display-only, no new inputs, no new external calls.

### Testing Strategy

- `test_heat.py`: per-field correctness against hand-computed fixtures; single-path
  invariant `compute_heat == heat_breakdown(...).overall`; existing float assertions
  unchanged.
- `test_web.py`: `/summarize` renders the strip with expected glyphs and formatted values;
  no-VIP thread omits 👑; VIP thread appends 👑; error branch renders no strip.
- Edge cases: `heat_breakdown(t, c)` with `now=None` equals the explicit-`now` call for the
  same instant (default-arg parity); `self_user_id=None` and a not-involved thread both
  yield `damping == 1.0` (the dimmed-no-op path); a monologue yields `velocity == 0.0` and
  the ⚡ chip still renders `0.0`.
- `otto ci` (whitespace + ruff format + ruff lint + mypy strict + pytest) green per phase.

Per repo convention (mirroring the fire-repurpose feature), each phase appends a section to
a companion `docs/design/2026-06-30-heat-metrics-strip-implementation-notes.md` recording
decisions, deviations, and any tradeoffs discovered during implementation.

### Rollout Plan

Single deploy via the normal `uv run`/Dockerfile path; no migration, no flag. Ship after
the review-panel implementation audit (it caught the v0.3.8 tone bug on the last feature).

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Breakdown refactor changes a score | Low | High | Single-path invariant test; keep all existing `compute_heat` assertions |
| Strip 🌡️ differs from row's last-ranked score confuses user | Med | Low | Documented as live-vs-poll; values are close and the strip is the more-current one |
| ⚡ shows nonzero while score unmoved (velocity_weight=0 default) | Med | Low | ⚡ chip dimmed when `velocity_weight == 0`; tooltip states contribution is `vel × velocity_weight` |
| Strip widens the popup past the dense ideal | Low | Med | One line, tabular-nums, terse chips, dimmed no-op slots; matches `.row-counts` density |
| Emoji rendering inconsistency across platforms | Low | Low | Use the same `\N{...}` glyph convention already proven on the row |

## Resolved Questions

- [x] **📊 base tooltip shows both** `base` and `people_term`, terse like the existing
      count tooltips (`threads.html:21`): `base 86.0 · people 6.0`. The `title=` costs zero
      resting pixels, and `people_term` next to `base` is precisely what makes the
      weighting/cap legible (the `Nm·Np` face cannot convey it). (Review-panel confirmed.)
- [x] **Dimmed no-op chips are always rendered, never hidden.** There is no responsive
      machinery in this codebase to hide against (`base.html` has zero `@media`
      breakpoints); the dense rows use fixed `min-width` + `white-space: nowrap` +
      `tabular-nums` and never reflow. The strip matches that: `nowrap` + `tabular-nums`,
      reduced opacity for no-ops, no hiding. This also preserves the fixed overall+5 layout
      goal. (Review-panel confirmed.)

## References

- `docs/design/2026-06-29-repurpose-fire-heated-exchange.md` (prior feature; doc style and
  glyph-reuse precedent) and its `-implementation-notes.md`.
- `src/slack_dashboard/heat.py:108-134` (`compute_heat`), `:50-105` (factor helpers).
- `src/slack_dashboard/web.py:381-414` (`/summarize`), `:26-32` (glyphs), `:116-119`
  (`_has_vip`).
- `src/slack_dashboard/templates/partials/summary.html`, `templates/base.html:9-97` (CSS).
- `CLAUDE.md` (density principle, no em dashes).
