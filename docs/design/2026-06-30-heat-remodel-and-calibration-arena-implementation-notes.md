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
