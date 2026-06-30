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
