# Implementation Notes: Heat Re-model + Autoresearch Calibration Arena

Design doc: `docs/design/2026-06-30-heat-remodel-and-calibration-arena.md`

## Phase 1: worktime.py + WorkWindowConfig

### Design decisions
- `WorkWindowConfig` placed as a nested `_KebabModel` in `config.py` and referenced from
  `HeatConfig.work_window` (kebab `heat.work-window`), NOT composed into `AppConfig` - matches
  the phase spec that pins the config shape here. `config.py:WorkWindowConfig`,
  `config.py:HeatConfig.work_window`.
- Weekday tokens stored as a module-level tuple `_WEEKDAY_TOKENS` indexed to match
  `datetime.weekday()` (Mon == 0 ... Sun == 6). `work_weekdays()` maps the configured
  `work_days` list to that integer set so `business_hours_between` can test membership in O(1).
  `config.py:_WEEKDAY_TOKENS`, `config.py:WorkWindowConfig.work_weekdays`.
- Validation lives in a single `@model_validator(mode="after")` (`config.py:WorkWindowConfig._validate`):
  rejects `end_hour <= start_hour`, empty `work_days`, unknown day tokens, and an unresolvable
  `timezone` (constructing a `ZoneInfo`). Fails clearly at boot per the phase's cheap-win #8.
- `business_hours_between` iterates LOCAL calendar dates in `work.timezone`, builds each work
  day's `[start_hour, end_hour)` window as aware local datetimes, intersects with the span, and
  converts endpoints to epoch via `.timestamp()` BEFORE subtracting - so a 23/25-hour DST day
  contributes its true wall-clock duration and no aware-datetime subtraction ever crosses a DST
  fold/gap. `worktime.py:business_hours_between`.
- Function-level DEBUG logging on `business_hours_between` (entry with all params, exit with the
  computed `work_hours`, plus the early-return-0.0 branch), per the logging rule. The per-day
  loop deliberately emits no per-iteration log (tight loop -> would be TRACE at most; kept silent
  since the entry/exit already tell the story).

### Deviations
- Added two small public helper methods to `WorkWindowConfig` not named in the phase spec:
  `work_weekdays()` (day-token list -> weekday-int set) and `tzinfo()` (resolved `ZoneInfo`).
  They keep `business_hours_between` a thin consumer and give Phase 2 a clean surface. Both are
  covered by tests. This is additive, not a behavior change to the specified API.

### Tradeoffs
- Iterate-local-dates + epoch-before-subtract vs. summing naive local-hour windows. The chosen
  approach is a few lines longer but is the only one that is DST-correct across a fold/gap;
  summing local hours would silently miscount a 23/25-hour day. Chosen for correctness (the
  phase's explicit DST discipline requirement).
- `tzinfo()` re-constructs `ZoneInfo` on each call rather than caching it on the model.
  `ZoneInfo` is itself cached at the stdlib level (interned per key), so this is cheap and keeps
  the pydantic model free of a non-serializable cached attribute. Chosen for model simplicity.

### Open questions
- None.

## Phase 2: Re-model the score in heat_breakdown

### Design decisions
- The whole re-shaped score lives in the ONE formula line in `heat.py:heat_breakdown`:
  `score = channel_weight * (base_norm + activity) * atrophy * alive_boost * damping`.
  `compute_heat` is still a thin wrapper over `.overall` (single-path invariant preserved and
  tested at `test_heat.py::test_single_path_invariant`).
- `base_norm` is a HARD asymptotic ceiling: `base_cap * volume / (volume + base_k)` -> `base_cap`
  as volume -> inf, monotone. `volume = message_count*reply_weight + people_term(capped)`. This
  is what lets a big stale thread fall below a small fresh one once atrophy applies.
  `heat.py:heat_breakdown`.
- `activity = min(activity_cap, velocity * velocity_weight)` is kept OUTSIDE the volume ceiling as
  its own bounded additive burst term, so a short active thread's spike is not washed out by a big
  thread's message-count saturation. `heat.py:heat_breakdown`.
- `atrophy = 0.5 ** (time_since_last / atrophy_half_life_work_hours)` - a true exponential
  half-life over WORKING hours via `worktime.business_hours_between(last_activity, now, work_window)`.
  Nights/weekends contribute 0, so a Friday-afternoon thread does not go stone-cold over the weekend.
  `heat.py:heat_breakdown`.
- `alive_boost = 1 + alive_weight * f(time_alive) * atrophy`, `f = time_alive/(time_alive+alive_k)`.
  The `* atrophy` gate means a long-lived thread is lifted only while fresh; once idle, atrophy -> 0
  collapses the boost back to ~1.0. `time_alive` is working hours from first-post (first_seen_ts, or
  float(thread_ts) fallback) to last-post. `heat.py:heat_breakdown`.
- `involvement_damping` reshaped into drop-and-rebuild:
  `damping = involved_drop + (1 - involved_drop) * min(1, messages_after * involved_rebuild_per_msg)`,
  clamped to `[involved_drop, 1.0]`. Posting drops the score to `involved_drop`; each unseen reply
  after the user's last post rebuilds toward 1.0. `involved_drop >= 1.0` disables it.
  `heat.py:involvement_damping`.
- `HeatBreakdown` gained `atrophy`, `activity`, `alive_boost`, `time_alive`, `time_since_last`
  (all fractional working hours for the two time fields) and the `recency` field was RENAMED to
  `atrophy`; `base` now carries `base_norm`. `heat.py:HeatBreakdown`.
- Tiering moved AFTER the sort in both call sites. `classify_tier(score, rank, total, config)` with
  a `tier-method` switch: absolute (score thresholds) or relative (rank-aware top-N with an absolute
  `tier_floor`, counts clamped to `min(count, total)`). `rank_threads` (`heat.py`) and
  `poller.ranked_threads` are now two-pass: score+sort, then classify over the sorted list. Neither
  re-implements the formula. `heat.py:classify_tier`, `heat.py:rank_threads`,
  `poller.py:ranked_threads`.
- `poller._update_heat` (per-entry ingest update, no board context) classifies the entry as rank 0
  of 1; the authoritative rank-aware tier is set post-sort in `ranked_threads` at render time.
  `poller.py:_update_heat`.
- DEBUG log in `heat_breakdown` extended to emit `volume`, `base_norm`, `activity`, `atrophy`,
  `alive_boost`, `time_alive`, `time_since_last`, and the selected `tier_method`. `heat.py:heat_breakdown`.
- Config migration: added `_migrate_tier_thresholds` (mirrors `_migrate_decay_half_life`) mapping
  legacy `hot-threshold`/`warm-threshold` onto the new `tier-hot`/`tier-warm`, and a
  `_validate_tier_method` after-validator. `config.py:HeatConfig`.
- Seed default knob values chosen (calibration tunes them in Phase 4):
  - `atrophy-half-life-work-hours` = 3.0 (per the doc's starting guess; 3 work-hrs -> 0.5, 12 -> ~0.06).
  - `base-cap` = 50.0 (keeps the historical hot-line scale: a well-populated thread approaches ~50).
  - `base-k` = 15.0 (half-saturation near volume 15, so the curve bends within everyday message counts).
  - `activity-cap` = 20.0 (a strong spike adds up to ~40% of a saturated base_norm, no runaway).
  - `alive-weight` = 0.0 (per doc: time-alive is DISPLAY-ONLY until calibration says otherwise).
  - `alive-k` = 6.0 (f half-point near ~6 work-hours of thread life).
  - `involved-drop` = 0.8 (a 20% cut, a bigger initial drop than the old 0.5 default).
  - `involved-rebuild-per-msg` = 0.15 (~7 unseen messages fully restores).
  - `tier-method` = "absolute" (per doc default).
  - `tier-hot` = 50.0, `tier-warm` = 20.0 (mirror historical lines, now meaningful given the ceiling).
  - `tier-hot-count` = 3, `tier-warm-count` = 10 (relative-mode top-N sizes, per doc).
  - `tier-floor` = 5.0 (chosen seed: absolute floor keeping a fully-atrophied board from painting
    top-N hot in relative mode; the doc left this open, so 5.0 is a starting guess for calibration).

### Deviations
- Removed the now-unused legacy involvement knobs `involved_damping`, `involved_decay_messages`,
  `involved_decay_hours` from `HeatConfig` (drop-and-rebuild fully replaces them). `hot_threshold` /
  `warm_threshold` were RETAINED (backward-compat + migration source) but are no longer read by the
  score; `decay_hours`/`decay_floor` were retained because `structural_heat` still consumes them.
- Updated `web.py:_heat_strip` to read `breakdown.atrophy` where it previously read
  `breakdown.recency` (the field rename), so the module still compiles. The strip's chip set /
  new time-alive+time-since-last chips are Phase 3 work; this was the minimal compile fix only.
- Test fixtures for the score were made deterministic with a pinned working-hours `now`
  (`_work_now`, Tue 2026-06-30 10:00 PT) and a `_work_thread` helper, because atrophy now depends on
  the position of `now` in the work window. Pre-existing `_make_thread`/default-now tests that only
  assert ORDERING (rank, single-path invariant) were left as-is since ordering holds regardless.

### Tradeoffs
- Field rename `recency -> atrophy` vs. keeping `recency` and adding `atrophy` alongside. Chose the
  rename: `recency` was the old linear-ramp semantics and keeping both would invite a second
  code path reading the stale name. web.py (the only external reader) was updated in the same change.
- `_update_heat` classifies as rank 0/1 (best-effort provisional) rather than deferring tier entirely
  to render. Chose provisional-then-overwrite so an entry always has a sane `heat_tier` between
  ingest and the next `ranked_threads`; the authoritative value is always the post-sort one.

### Open questions
- `tier-floor` seed (5.0) is a guess; the doc leaves it open and Phase 4 calibration should set it.
- `base-cap`/`base-k`/`activity-cap`/`alive-k` seeds are reasonable starting shapes but are the
  primary knobs the calibration arena (Phase 4) is meant to tune against the live board.

## Phase 3: Strip + docs wiring

### Design decisions
- Added two new strip-only glyph constants beside the existing heat-strip glyphs:
  `_TIME_ALIVE` (`\N{HOURGLASS}` ⌛) and `_TIME_SINCE_LAST` (`\N{TIMER CLOCK}` ⏲), distinct
  from `_RECENCY` (`\N{STOPWATCH}` ⏱️, the atrophy multiplier itself) so the strip can show
  the raw working-hours inputs alongside the decay they drive. `web.py` (glyph constants,
  ~line 45-49).
- `_heat_strip` now emits 7 chips (was 5): overall, channel_weight, base, velocity,
  time_alive, time_since_last, atrophy, damping - placed the two new chips adjacent to the
  atrophy chip they feed, per the doc's "one line, factors that compose it, expanding left
  to right" strip convention. `web.py:_heat_strip`.
- Both new chips render `{:.1f}h` (one decimal place, `h` suffix) - one more decimal than
  the existing `⏱️ {:.2f}` atrophy multiplier, since these are hour magnitudes (can run into
  double digits) rather than a bounded 0..1 ratio; `h` disambiguates the unit at a glance
  (the strip has no other bare-hour value to confuse it with).
- Dimming: `time_alive` chip dims when `config.alive_weight == 0.0` - the exact same
  convention the `⚡` velocity chip already uses for `velocity_weight == 0.0` (a chip dims
  when its knob is the multiplicative no-op default, per the strip doc's dimming
  conventions). This is honest: at the shipped `alive_weight: 0.0` seed, `alive_boost` is
  always 1.0, so `time_alive` is display-only exactly as the design doc states ("0 =
  display-only to start"). `time_since_last` is NEVER dimmed - it is the direct input to
  `atrophy`, which is always live regardless of any knob's value, so there is no no-op
  state to signal. `web.py:_heat_strip`.
- `slack-dashboard.example.yml` `heat:` block rewritten to document every Phase 2 knob with
  the seed defaults read directly from `config.py` (not from the design doc's prose, to
  avoid transcription drift): `work-window` (timezone/start-hour/end-hour/work-days),
  `atrophy-half-life-work-hours` 3.0, `base-cap` 50.0, `base-k` 15.0, `activity-cap` 20.0,
  `alive-weight` 0.0, `alive-k` 6.0, `involved-drop` 0.8, `involved-rebuild-per-msg` 0.15,
  `tier-method` absolute, `tier-hot` 50.0, `tier-warm` 20.0, `tier-hot-count` 3,
  `tier-warm-count` 10, `tier-floor` 5.0. Comment style matches the file's existing
  formula-in-comment convention (e.g. the pre-existing heated-exchange block).
- Removed the now-superseded `hot-threshold: 50` / `warm-threshold: 20` lines from the
  example (they were the pre-remodel absolute tier knobs); the new `tier-hot: 50.0` /
  `tier-warm: 20.0` under the `tier-method` block are their direct, currently-read
  replacements, so documenting both would show two config surfaces for the same value.
  `hot-threshold`/`warm-threshold` remain valid, working legacy keys in `config.py`
  (`_migrate_tier_thresholds`) - only the example file's *documented* surface changed, no
  code/behavior change.
- Also documented `decay-hours`/`decay-floor` as legacy (still read by `structural_heat`'s
  decay term, per Phase 2 notes, but no longer part of the ranking `atrophy` calculation)
  so the comment does not mislead a reader into thinking they still drive the main score.

### Deviations
- None. No new config fields were added (per phase scope); this phase only wired display
  (chips/glyphs/CSS) and documentation (example.yml comments/defaults).

### Tradeoffs
- `{:.1f}h` unit-suffixed format for the two new chips vs. matching the bare `{:.2f}`
  numeric style of the existing atrophy/velocity/damping chips exactly. Chose the `h`
  suffix because these two values are literally hour counts (unlike the existing chips,
  which are all ratios/multipliers/rates already disambiguated by their glyph), and an
  unlabeled `5.2` beside `⌛` invites the reader to guess units; the tooltip states the unit
  too, but the face value should not require a hover to parse, per the density principle
  (scannable at rest, hover is for precision/provenance, not for basic legibility).
- Placed the two new chips between velocity and atrophy (their natural formula
  neighbors) rather than appending them at the end of the fixed 5-chip layout. Chose
  formula-adjacency over pure additive-append because the strip's stated purpose is "why
  a thread sits where it does" - `time_since_last` reads naturally right before the
  `atrophy` multiplier it produces, and `time_alive` similarly explains the (currently
  dimmed) `alive_boost` interplay; grouping them elsewhere would separate a value from the
  factor it explains. The base.html/heat_strip.html rendering is glyph-order-agnostic (a
  flat iteration over `heat`), so this reordering carries zero template risk.
- Did not touch CLAUDE.md's chip vocabulary section in this phase, even though it now
  undercounts the strip at "5 chips" - the design doc's Implementation Plan explicitly
  assigns "Update CLAUDE.md chip vocabulary + strip tooltips for the new chips" to Phase 5
  ("Lock + freeze"), not Phase 3. Fixing it here would be gold-plating outside this
  phase's stated scope.

### Open questions
- None.
