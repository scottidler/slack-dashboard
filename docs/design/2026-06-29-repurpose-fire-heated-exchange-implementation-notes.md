## Phase 1: groundwork

### Design decisions

- `reply_timestamps` made a `@property` on `ThreadEntry` (derived projection of `replies`) rather than a stored field - `thread.py:ThreadEntry.reply_timestamps` - single source of truth; eliminates dual-write drift across the three ingestion paths.
- `merge_replies` uses `f"{r.ts:.6f}"` string keys matching `prune_timestamps` normalization - `thread.py:merge_replies` - ensures sub-ulp float differences from socket vs REST round-trips collapse to one record, preserving the existing dedup guarantee.
- `structural_heat` returns 0 immediately when `distinct(authors) < 2` (monologue guard) before any other computation - `heat.py:structural_heat` - avoids dividing by near-zero and matches the spec's "a monologue is never heated" contract explicitly.
- `is_heated` uses `tone_term = thread.heated_tone * config.heated_tone_weight` (0 in Phase 1 since `heated_tone=0`) - `heat.py:is_heated` - the formula is complete and correct for Phase 2; only the LLM emit/parse is missing.
- `SummaryResult` dataclass added with `bullets: str | None` and `tone: int = 0` - `llm/provider.py:SummaryResult` - callers check `result.bullets is None` for failure rather than `result is None`; this is a cleaner failure mode than the prior bare string return.
- `unanswered_max_replies` default bumped from 2 to 3 - `config.py:HeatConfig` - `message_count` now includes the root message, so "root + up to 2 replies" maps to `<= 3` not `<= 2`. All tests explicitly pass `max_replies` so no test regressions; the config.py default is the only change.
- `message_count=len(replies)` in full fetch (drop the `-1`) - `slack/poller.py:_fetch_thread` - Slack's `conversations_replies` returns root + replies; the old `-1` was stripping the root. `message_count` now counts all messages including root, matching `participants` which already included the root author.

### Deviations

- `test_config.py::test_defaults` updated to assert `unanswered_max_replies == 3` instead of `2` - the default changed per the "threshold touch-ups" section of the design doc which explicitly called for re-eyeing this value.
- Full fetch `message_count` becomes 4 in `test_poller.py` fixture (root + 3 replies = 4, not 3) - the spec says "drop the -1", so all poller tests asserting `reply_count == 3` now assert `message_count == 4`. This is correct behavior not a deviation.

### Tradeoffs

- `reply_timestamps` as a `@property` vs keeping as a stored field with merge_replies updating it - property approach chosen because it guarantees the derived value is always consistent with `replies`; a stored field would require callers to keep both in sync across all three ingestion paths (the exact dual-write problem the design doc identifies as the root cause of prior bugs).
- `MAX_REPLY_TIMESTAMPS = 500` kept in `heat.py` as a backward-compat constant alongside `MAX_REPLY_RECORDS = 500` in `thread.py` - kept for clarity since `prune_timestamps` still references it, even though it is no longer the primary cap (the cap is now on `replies` via `merge_replies`). Could be removed in a future cleanup but leaving it avoids a spurious naming change.
- Incremental path in `poller.py` does NOT rebuild the full `ThreadEntry` - it mutates the existing entry in-place (adding participants, bumping `message_count`, merging reply records). This matches the prior behavior and avoids losing in-memory state (title watermark, summary, heated_tone) that a full rebuild would require explicitly carrying.

### Open questions

- None.
