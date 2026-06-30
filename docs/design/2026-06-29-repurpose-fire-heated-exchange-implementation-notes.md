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

## Phase 2: the repurpose

### Design decisions

- TONE parsing extracted into a standalone `parse_tone(text) -> (bullets, tone)` helper rather than inlined in `generate_summary` - `llm/provider.py:parse_tone` - keeps the emit (prompt), call, and parse responsibilities separable and unit-testable without an LLM mock.
- The TONE regex `(?im)^\s*TONE:\s*(-?\d+).*?$\s*\Z` anchors to the END of the response (`\Z`) - `llm/provider.py:_TONE_RE` - so a stray "TONE:" inside the bullet body cannot be mistaken for the trailing rating line; case-insensitive per CLI/enum convention (the model may echo "tone:" lowercase).
- Unparseable TONE (e.g. "TONE: high") leaves the line intact in bullets and falls back to tone 0 - `llm/provider.py:parse_tone` - rather than stripping a line we could not interpret; the no-digit case is rare and keeping the text is the safer "never block on tone" behavior. Missing TONE entirely also keeps all bullets.
- Warm tier color nudged from `#f39c12` (orange) to `#f1c40f` (clear amber/yellow) - `templates/base.html` - per Resolved Decision 1; once 🔥 no longer reinforces the hot tier, text color is the sole tier cue and the old orange sat too close to hot red.
- `_emojis` passes the request-captured `now` into `is_heated(thread, config.heat, now)` - `web.py:_emojis` - so all rows in a request share one timestamp (the same discipline already applied to the new/zombie glyphs), keeping decay consistent across the page.
- `web.py` summary route now stores `entry.heated_tone = result.tone` alongside the summary watermark - `web.py` (summarize route) - matching `main.py:on_summary_needed` so tone is persisted on both the background and hover-driven (re)summary paths.

### Deviations

- The design doc lists `tone term` etc. logging "in `is_heated`"; that logging already landed complete in Phase 1, so Phase 2 only refreshed the now-stale Phase-1 docstrings in `heat.py` (`is_heated`) - no logic change to the score math, which Phase 1 had already written to its Phase-2-complete form (`heated_score = structural_term + tone_term`).
- `slack-dashboard.example.yml` still shows `unanswered-max-replies: 2` (Phase 1 bumped the code default to 3). Left untouched - it is outside Phase 2 scope (the 🔥/heated repurpose) and the design doc's example.yml edit for Phase 2 is specifically the line-48 "fire = hot tier" comment plus the new `heated-*` block, both of which are done.

### Tradeoffs

- Rewrote the route-level `test_threads_renders_fire_emoji_for_hot` into `test_threads_renders_fire_emoji_for_heated` with a locally-built heated thread instead of mutating the shared `_make_thread()` fixture - chosen to avoid changing the seven other tests that consume that fixture and rely on it NOT firing 🔥 (it is a cordial/no-reply thread by default).
- `_make_spiking_thread` gained a `heated_tone` param (monologue keeps structural 0) so the ⚡/🔥 coexistence tests drive fire purely via tone - vs building a separate two-author fixture for each - keeping the spiking tests' intent (the two signals are independent) crisp.

### Open questions

- None.

## Follow-up: Implementation Audit fixes (post-v0.3.7, shipped v0.3.8)

The review-panel implementation audit (Architect/Gemini + Staff Engineer/Codex) on the
shipped v0.3.7 surfaced two real findings that undercut the tone signal. Both fixed here.
This section supersedes the two "Open questions: None." entries above for the audit's scope.

### Design decisions

- Added `ThreadEntry.summary_texts` property - `thread.py` - sourcing the LLM summary/tone
  input from the canonical retained `replies` record (full ordered exchange, root + replies),
  falling back to `[first_message]` only when no records are retained yet. Both summary-bearing
  paths (`web.py` summarize route, `main.py:on_summary_needed`) now feed `summary_texts`, so
  tone is rated on the whole conversation on every path.

### Deviations

- None from the design doc - the doc's premise ("`generate_summary` already sends the full
  thread") was factually false about the existing code; this fix makes reality match the
  doc's stated intent rather than departing from it.

### Tradeoffs

- Kept the two summary call sites (web route renders templates, `on_summary_needed` is
  fire-and-forget) rather than extracting a shared helper - they share the now-tested
  `summary_texts` property and the `is not None` contract, but their surrounding behavior
  (template branch vs. background mutation) differs enough that a shared async helper would
  have widened the diff for little gain.

### Open questions

- None.

### Audit findings addressed

1. **MUST-FIX - tone rated on root-only/delta text, not the full exchange.** The hover
   summary path (`web.py`) and background re-summary (`main.py:on_summary_needed`) previously
   passed only `first_message` / the triggering delta to `generate_summary`, so a hostile
   back-and-forth under a polite root scored `tone=0`. Both now feed `entry.summary_texts`
   (the full retained `replies`). Covered by `test_summarize_feeds_full_retained_exchange_to_llm`
   and the `summary_texts` property tests.
2. **CHEAP-WIN - `main.py:on_summary_needed` violated the `SummaryResult` failure contract.**
   It used truthiness (`if result.bullets:`), so a parseable TONE-only response (`bullets=""`,
   `tone=3`) dropped both summary and tone - exactly the strong-hostility signal the feature
   exists for. Changed to `if result.bullets is not None:`, matching the web route. Covered by
   `test_summarize_preserves_tone_when_bullets_empty`.

Findings 3 (`example.yml` `unanswered-max-replies: 2`) and 4 (`TONE:` strip only handles
numeric lines, deliberate) were assessed defer/non-issue and left as-is.

## Follow-up: 🔥 → 🌶️ reglyph + 👤 involved signal (shipped v0.3.9)

Two user-directed changes after v0.3.8, implemented directly (no separate design doc).

### Design decisions

- **Heated-exchange glyph moved from 🔥 to 🌶️** (`web.py:_PEPPER = "\N{HOT PEPPER}"`). The
  signal/logic is unchanged (`is_heated`); only the rendered glyph, legend, tooltip,
  `example.yml`, and the now-stale 🔥 comments in `config.py`/`base.html` changed. Note:
  Unicode has no jalapeño codepoint; 🌶️ (HOT PEPPER) is the standard "spicy" glyph.
- **New 👤 involved glyph** (`web.py:_INVOLVED = "\N{BUST IN SILHOUETTE}"`): fires when the
  authenticated user has personally posted in a thread. This was an original 2026-06-29 ask
  ("I also want one for if I am in the thread or not") that was dropped when that session
  narrowed to the fire repurpose; resurfaced and built here.
  - `heat.is_involved(thread, self_user_id)` - membership is a plain lookup against
    `participants` (keyed by stable user_id, includes every author). The v3.2 reply-record
    work is what made this trivial.
  - Self-user resolution: `SlackClient.resolve_self()` calls `auth.test` once; `SlackPoller`
    stores it in `start()` as `self_user_id`. The render path threads it through
    `group_threads`/`_build_row`/`_emojis` exactly like `app_start_at`.
  - **Render order: 👤 leads** (leftmost) as the primary triage cue ("am I already in this?").

### Deviations

- None.

### Tradeoffs

- Threaded `self_user_id` as an explicit param through the render path (mirroring
  `app_start_at`) rather than stashing it on a global or mutating config - keeps the render
  functions pure and testable, at the cost of touching several signatures.

### Open questions

- None.

### Failure mode

- `auth.test` failure (network/invalid token) leaves `self_user_id` None; `is_involved`
  then never matches, so the 👤 glyph simply stays dark - no thread is blocked, nothing
  crashes. Covered by `test_resolve_self_none_on_failure` and `test_is_involved_false_when_self_unresolved`.

## Follow-up: involvement damping (shipped v0.3.10)

A heat-ranking change requested after the 👤 glyph: a thread the user has RECENTLY posted
in is lower priority (they already weighed in), with the reduction fading as the message is
buried and as time passes. The 👤 presence glyph is unchanged; this only scales heat.

### Design decisions

- `heat.involvement_damping(thread, config, self_user_id, now_ts)` returns a multiplier in
  `[1 - involved_damping, 1.0]`, folded into `compute_heat` as the last factor (alongside
  channel_weight and recency). Uses the user's last retained post in `replies` (author_id ==
  self), counts messages after it, and computes `freshness = msg_fade * time_fade`;
  `damping = 1 - involved_damping * freshness`.
- **Product of the two fades** (message-burial AND time), not min/max: both must be fresh for
  full reduction; either one going stale erodes it. Each axis has its own decay knob, and a
  knob of 0 disables that axis (fade fixed at 1.0). This is the "knobs for power and
  diminution" the user asked for.
- Three knobs on `HeatConfig` (kebab in yaml, load from the XDG config via `_KebabModel`):
  `involved-damping` (peak cut, default 0.5), `involved-decay-messages` (default 10),
  `involved-decay-hours` (default 24.0). Documented in `slack-dashboard.example.yml` and added
  to the live `~/.config/slack-dashboard/slack-dashboard.yml`.
- `self_user_id` reaches `compute_heat` from the poller (`ranked_threads`, `_update_heat`)
  the same way the render path gets it; `rank_threads` gained an optional pass-through.

### Deviations
- None.

### Tradeoffs
- Multiplicative damping on the final score (not subtracting from base) keeps it explainable
  and composable with the other multipliers, and bounds the effect to a clean fraction.

### Open questions
- Defaults (0.5 / 10 msgs / 24h) are a starting point; calibrate by observation.
