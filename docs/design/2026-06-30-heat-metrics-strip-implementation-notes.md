# Implementation Notes: Heat-Metrics Strip in the Thread Hover Popup

Companion to `docs/design/2026-06-30-heat-metrics-strip.md`. Each phase appends a section
(append-only); never edit a prior phase's entry.

## Phase 1: HeatBreakdown + heat_breakdown() refactor

### Design decisions
- `HeatBreakdown` is a `@dataclass(frozen=True)` - `heat.py:HeatBreakdown` - so the
  composed factors are an immutable value object; nothing downstream can mutate a computed
  breakdown, and frozen matches the read-only display use in later phases.
- `is_vip` lives in `heat.py` and takes a `HeatConfig` (not `AppConfig`) - `heat.py:is_vip`
  - so the single VIP rule sits beside the score math that consumes it. `web._has_vip`
  (`web.py:_has_vip`) now delegates with `return is_vip(thread, config.heat)`, removing the
  duplicate rule.
- `heat_breakdown` derives `hours_since` from the float `now` via
  `(now - thread.last_activity.timestamp()) / 3600` - `heat.py:heat_breakdown` - matching
  the `structural_heat` pattern (`heat.py:236`) rather than the datetime subtraction the old
  `compute_heat` used. For a tz-aware `last_activity` (the poller creates it aware) these are
  arithmetically identical, so no score changes; the float form is what lets the route pass a
  request-captured `now` in Phase 2.
- `compute_heat` is now a one-line wrapper - `heat.py:compute_heat` - `return
  heat_breakdown(thread, config, self_user_id).overall`. Signature unchanged, so
  `rank_threads` (`heat.py:rank_threads`) and the poller (`poller.py:99-109`) are untouched.
- Removed the now-unused `resolve_person_weight` import from `web.py` (it was only used by
  the old `_has_vip` body) to keep ruff lint clean.
- Added a DEBUG entry log at `heat_breakdown` start (function name + channel, thread_ts,
  message_count, participant count, self_user_id, now) and moved the old `compute_heat` exit
  DEBUG block into `heat_breakdown`, extended to log message_count, people_count, people_term,
  and has_vip - per `rules/logging.md`.

### Deviations
- None. (The implementation matches the doc's Phase 1 spec, including the float-`now`
  handling, the `is_vip` delegation, and the thin-wrapper `compute_heat`.)

### Tradeoffs
- Single-path invariant tested via default-`now` parity with a small tolerance (`< 1e-3`)
  rather than exact float equality - `test_heat.py:test_single_path_invariant`. Because
  `compute_heat` and `heat_breakdown` each capture their own wall-clock `now`, a sub-second
  decay difference on a 24h curve is unavoidable; pinning a shared `now` and asserting exact
  equality is the strict form and is also covered (the wrapper is deterministic for a fixed
  `now`). The tolerance form documents the live-recompute reality; the pinned form proves the
  arithmetic is one path.
- Kept `is_vip` as a free function rather than a method on `HeatBreakdown` so `web._has_vip`
  can delegate without constructing a full breakdown (the row path only needs the boolean).

### Open questions
- None.

## Phase 2: Wire the breakdown into /summarize and render the strip

### Design decisions
- New glyph constants added next to the existing block - `web.py:_THERMO`,
  `web.py:_CHANNEL_WEIGHT`, `web.py:_BASE`, `web.py:_RECENCY` - using the same `\N{...}`
  form as the row glyphs; verified each Unicode name resolves
  (`unicodedata.lookup("THERMOMETER")` etc.) before relying on it. `_SPIKING`, `_INVOLVED`,
  and `_VIP` are reused unchanged for velocity, damping, and the base chip's crown.
- Introduced a `HeatChip` dataclass (`web.py:HeatChip`) and a `_heat_strip(breakdown,
  config, channel_name)` helper (`web.py:_heat_strip`) that builds the fixed
  overall+5-chip list with already-formatted face values, native tooltip text, and the
  `dimmed` flag. All formatting/dimming logic lives in Python per the doc's instruction;
  `templates/partials/heat_strip.html` only iterates `chip.glyph` / `chip.value` /
  `chip.tooltip` / `chip.dimmed`.
- The `/summarize` handler (`web.py:summarize`) captures `now = datetime.now(UTC)
  .timestamp()` and calls `heat_breakdown(entry, config.heat, poller.self_user_id, now)`
  immediately after the `entry is None` guard, then builds `detail` with a `"heat"` key
  carrying the chip list. All three thread-bearing branches (cached, fresh-LLM-success,
  fresh-LLM-failure) spread `**detail`, so the strip is present in all three; only the
  missing-thread branch omits it (the doc's exact contract).
- `summary.html` now `{% include "partials/heat_strip.html" %}` guarded by `{% if heat
  %}` in both the error layout and the normal layout, so the strip renders above either
  the "Failed to generate summary / Retry" block or the quote+bullets block, and the
  missing-thread error (no `heat` in context) shows only the error - matching the doc's
  "both layouts" requirement without duplicating the chip markup inline in
  `summary.html`.
- Pulled the strip's `<div class="heat-strip">...</div>` markup into its own partial
  (`templates/partials/heat_strip.html`) rather than inlining the `{% for %}` twice in
  `summary.html` (once per branch) - one include, used from both branches, avoids
  duplicating the chip-rendering markup.
- Bold quote / normal-weight author is CSS-only (`base.html:.thread-quote` /
  `.thread-quote-author`), per the doc - no markup wrapping added in `summary.html`.
- `.heat-strip` / `.heat-chip` / `.heat-chip.dim` added to the inline `<style>` in
  `base.html`, matching `.row-counts`' density (`tabular-nums`, `white-space: nowrap`,
  terse `gap`). No `@media` breakpoints, per the Resolved Questions - chips always
  render, dimmed ones just go to `opacity: 0.4`.
- The 🏷️ channel-weight chip's tooltip includes the `#channel` name (`f"#{channel_name}
  channel weight"`) per the doc's "channel name + weight" spec; the 📊 base chip's
  tooltip is the terse `base {base:.1f} · people {people_term:.1f}` form lifted directly
  from the doc's Resolved Questions example.

### Deviations
- The ⏱️ recency tooltip text is "hours since last activity, decayed toward the floor"
  rather than the doc's literal placeholder "hours since last activity ..." (an
  ellipsis, not finished prose) - filled in with a complete, accurate description of
  what the `recency` multiplier represents (`heat.py:heat_breakdown`'s `recency = max
  (decay_floor, 1.0 - hours_since/decay_hours)`), since shipping a literal "..." would
  be a worse tooltip than a real sentence. No behavior or formatting change, doc intent
  preserved.

### Tradeoffs
- Built a dedicated `templates/partials/heat_strip.html` partial instead of inlining the
  chip loop directly in `summary.html`'s two branches - the doc only specified "render a
  `.heat-strip` block... in both layouts" without mandating a separate file. A shared
  include avoids two copies of the same `{% for chip in heat %}` markup drifting apart,
  at the cost of one more small template file; chosen for the same single-source
  discipline the doc applies to the score math.
- `_heat_strip` takes `channel_name` as a plain `str` (read from `entry.channel_name`)
  rather than threading the whole `ThreadEntry` through - the helper only ever needs the
  name for the tooltip, so the narrower signature is easier to test in isolation
  (confirmed via a direct unit-level check during implementation, then covered through
  the route-level mock-poller test pattern already established in `test_web.py`,
  consistent with the existing helpers like `_emojis`/`_build_row` that also take
  individual fields rather than full configs where only a few fields are used).

### Open questions
- None.

## Phase 3: Polish and convention

### Design decisions
- Verified rather than re-implemented: Phase 2 already satisfied both of Phase 3's
  concrete checks, so no production code changed.
  - **`now` discipline** (`web.py:summarize`): the handler captures exactly one
    `now = datetime.now(UTC).timestamp()` right after the `entry is None` guard
    (`web.py:478`) and passes that single value into `heat_breakdown(entry, config.heat,
    poller.self_user_id, now)` (`web.py:479`). There is no second wall-clock read inside
    the handler; the same `now` would also feed any later use in this request if one were
    added. This matches the existing pattern at `web.py:441` (`/threads`) and `web.py:515`
    (`/channel/{channel_id}`), which each capture one `now` per request.
  - **CSS** (`templates/base.html:101-103`): `.heat-strip` carries both
    `font-variant-numeric: tabular-nums` and `white-space: nowrap`, matching
    `.row-counts` (`base.html:70`). `.heat-chip.dim` uses `opacity: 0.4`, in the same
    range as the codebase's other de-emphasis opacities (`.count-sep` at 0.45,
    `.group-sep` at 0.6) - dimmed enough to read as "no-op for the score" while the glyph
    and value stay legible at a glance.
- Documented the chip vocabulary in `CLAUDE.md` under "Design principle: maximum
  information density", as a new bullet alongside the existing hover-affordance bullets
  (`CLAUDE.md` density section) rather than a separate subsection - it is one more hover
  affordance (the title hover), so it reads naturally as part of that existing list rather
  than as a freestanding heading. Glyph -> factor mapping spelled out tersely (🌡️ overall,
  🏷️ channel weight, 📊 base with 👑, ⚡ velocity, ⏱️ recency, 👤 damping), and the note that
  ⚡/👤/👑 are reused from the row's emoji column rather than newly invented.

### Deviations
- None. Both Phase 3 verification items passed as committed in Phase 2; no code change
  was required for either. The only Phase 3 deliverable that needed new work was the
  `CLAUDE.md` documentation.

### Tradeoffs
- Considered adding a dedicated "## Heat-metrics strip vocabulary" subsection in
  `CLAUDE.md` versus folding it into the existing hover bullet list - chose the latter
  for terseness and consistency with how the other hover affordances (title, #channel,
  Nm/Np) are already documented in that same list rather than broken out, keeping the
  density section itself dense.

### Open questions
- None.
