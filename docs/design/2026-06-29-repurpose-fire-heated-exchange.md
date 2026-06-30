# Design Document: Repurpose 🔥 - Fire as "Heated Exchange" (Triage v3.2)

**Author:** Scott Idler
**Date:** 2026-06-29
**Status:** Implemented - Design Review pass 2 complete (2026-06-29, Architect/Gemini + Staff
Engineer/Codex). Pass 1: 3 blockers + majors. Pass 2: paperwork findings (return-type, parse
contract) closed; the three substantive findings (tone input, score math, back-and-forth) were
described but not supported by the data model. This revision fixes the **root cause** both
reviewers converged on - a single retained, ordered reply record - and the score math. Ready for
re-review.
**Builds on:** `2026-06-27-emoji-signals-and-observation-store.md` (v3.1, implemented). This
doc repurposes one of that doc's glyphs.

## Summary

🔥 currently means `heat_tier == "hot"` (`web.py:179`). The row title is *already* colored by
the same `heat_tier`: `.heat-hot` is red, `.heat-warm` orange, `.heat-cold` blue
(`base.html:10-12`). So the fire glyph and the red title are the **same signal from the same
source** - the glyph is pure redundancy and, by the density principle ("does this earn its pixels,
or can it be a hover?"), it does not earn its width.

This doc reclaims 🔥 for a signal the dashboard lacks: **a heated exchange** - frustration,
conflict, escalation - orthogonal to volume (heat). A thread can be hot but cordial, or low-volume
but hostile. 🔥 fires on a single **heated score** = structural term **+** tone term, both summed
(this is "yes, and"); each is normalized to 0-10 and recency-decayed so neither dominates or
lingers:

- **Structural intensity** - the *shape* of a fight: a real back-and-forth (alternating authors)
  between few people, fired off fast, *and recent*. Deterministic, render-time, no LLM.
- **Linguistic tone** - the *words*: is the language contentious. Rides the per-thread Anthropic
  summary call (`provider.py:53`) as one extra returned field - no new request.

**The enabling change (panel pass 2, both reviewers' hardest question).** Both signals depend on
data `ThreadEntry` does not retain today: per-reply **text** (for tone) and per-reply **author
order** (for back-and-forth). `participants` is an unordered `dict[str,int]`; `reply_timestamps`
is a bare float list. So this design's foundation is **one capped, ordered, ts-keyed reply
record** with a single merge/dedupe path - see [Retained reply record](#retained-reply-record). It
unblocks tone input, back-and-forth, and bounded memory at once.

**Phasing (panel finding 6):** a structural-only "heated" glyph is hard to distinguish from the
existing ⚡ velocity signal. So **🔥 keeps `heat_tier == "hot"` through Phase 1** (groundwork
only - the record, the plumbing, the structural metric computed but not driving the glyph). The
repurpose flips on in **Phase 2**, atomically with tone, warm color, and the legend.

## Problem Statement

### Background

The emoji state channel (`web._emojis`, `web.py:121`) is the dashboard's high-density signal
surface. 🔥 is defined as `heat_tier == "hot"` (`web.py:178-179`); the title is colored by the
same tier (`threads.html:40`, `base.html:10-12`). Both read the single source of truth
`thread.heat_tier` from `classify_tier()` (`heat.py:136`), so **red title ⟺ 🔥, always.**

`ThreadEntry` (`thread.py`) retains, relevant here: `participants: dict[str,int]` (counts,
**unordered**), `reply_timestamps: list[float]` (deduped/capped at `MAX_REPLY_TIMESTAMPS = 500`,
`heat.py:11`, velocity-window pruned), `first_message: str`. It does **not** retain reply text or
author order; those exist only transiently during fetch (`poller.py:295-301`).

### Problem

1. **🔥 is redundant** - duplicates the title color exactly.
2. **No "heated" signal exists.** Heat is *volume × recency × weight*; a contentious thread may be
   low heat and get buried - the orthogonality that made "Unanswered" valuable in v3.1.
3. **Color already covers the bands** once warm reads clearly vs. hot red.

### Goals

- Repurpose 🔥 to **heated exchange**, decoupled from `heat_tier`.
- Drive it from one **heated score** = structural + tone, both normalized to 0-10 and
  recency-decayed; glyph fires above a configurable threshold.
- Structural term **explainable, render-time**; tone term **free** (rides the summary call).
- Provide the **retained reply record** both terms need, with bounded memory and a single
  merge/dedupe path across all ingestion routes.

### Non-Goals

- Changing the heat *ranking* or title-color meaning (hot/warm/cold stays volume-based).
- A separate LLM request for tone.
- Sentiment beyond "is this heated."

## Design

### Retained reply record

The foundation. Add one ordered, capped, deduped record and make it canonical for everything that
needs reply timing/authorship/text:

```python
@dataclass
class ReplyRecord:
    ts: float        # Slack ts; the dedupe key (normalized to 6dp, as prune_timestamps does)
    author_id: str   # stable Slack user_id
    text: str        # truncated to REPLY_TEXT_MAX chars (e.g. 280) - bounds memory + LLM tokens
    is_root: bool     # root message vs reply

# on ThreadEntry:
replies: list[ReplyRecord] = field(default_factory=list)   # sorted by ts, deduped, capped
```

**One merge path, used by all three ingestion routes** - this is the dedupe contract the prior
draft lacked (panel blocker 1/2):

```python
def merge_replies(existing: list[ReplyRecord], incoming: list[ReplyRecord]) -> list[ReplyRecord]:
    by_key: dict[str, ReplyRecord] = {f"{r.ts:.6f}": r for r in existing}
    for r in incoming:
        by_key[f"{r.ts:.6f}"] = r            # latest wins for a given ts; strings dedupe by key
    merged = sorted(by_key.values(), key=lambda r: r.ts)
    return merged[-MAX_REPLY_RECORDS:]        # cap (e.g. 500), drop oldest
```

- **Full fetch** (`poller.py` ~362): merge the full reply set.
- **Incremental refresh** (`poller.py` ~290): merge only `new_replies`.
- **Socket listener** (`listener.py` ~75): currently appends *only* a timestamp - extend it to
  carry `author_id` + `text` for the single live event, then `merge_replies`.

`reply_timestamps` becomes a **derived projection** of `replies` (one source of truth, no
dual-write drift): `[r.ts for r in replies]`. Velocity/`prune_timestamps` consume the projection
unchanged. Text is truncated per record, so worst-case memory is `MAX_REPLY_RECORDS × REPLY_TEXT_MAX`
per thread (~140 KB at 500×280) - bounded, and only retained for surfaced threads.

> Per the logging rule: never log full reply texts - log counts / length previews only.

### The heated score

```
heated_score = structural_term + tone_term          # both in 0..10, comparable
🔥 fires when heated_score >= heated_threshold
```

**Structural term** (`heat.py`, render time, from `replies`). The pass-2 math fixes: gate velocity
by the exchange ratio so a fast *monologue* or fast *civil* thread cannot fire on raw velocity
(kills the ⚡ overlap), and clamp **before** a floor-free decay so an old fight actually cools to 0
(kills the "pins at 10 forever" burn):

```
authors = [r.author_id for r in replies ordered by ts]      # the record gives us order
n = len(authors)
if distinct(authors) < 2:        structural_term = 0          # a monologue is never "heated"
alternations = count(i where authors[i] != authors[i-1])
exchange  = alternations / max(1, n - 1)                      # 0..1: real back-and-forth
volume    = message_count + replies_in_window(thread)         # raw activity (see message-count change)
intensity = exchange * volume                                 # GATED by exchange -> not raw ⚡
capped    = min(10.0, intensity * heated_structural_scale)    # clamp FIRST -> 0..10
decay     = max(0.0, 1.0 - hours_since_last / decay_hours)    # NO floor -> reaches 0 with age
structural_term = capped * decay                              # 0..10, decays to 0
```

A fast civil thread has `exchange` high but tone low; a fast monologue has `exchange ≈ 0` →
`intensity ≈ 0` → no 🔥. The decay has no `decay_floor` (that floor is a `compute_heat` concern,
`heat.py:67`); here it must reach 0 so resolved fights stop firing.

**Tone term** (LLM, stored on the thread). `generate_summary` already sends the full thread; it is
extended to return a **tone score 0-3** ("0 cordial, 1 tense, 2 pointed/frustrated, 3 openly
hostile/escalating") - 0-3 not 0-10 for LLM consistency. Stored on `ThreadEntry.heated_tone` on
(re)summary via the existing `summary_watermark` cadence.

```
tone_term = heated_tone * heated_tone_weight       # 0..3 * 3.0 = 0..9
```

No summary yet → `tone_term = 0`; the thread can still fire on structure and gains tone once
summarized. **No thread is ever blocked on the LLM.**

### LLM changes (blocker 3 + major 7, the closed findings, restated)

- `generate_summary` returns a **`SummaryResult`** dataclass (`bullets: str | None`,
  `tone: int`), not a bare tuple/string. Abstract signature (`provider.py` ~25) changes with it.
- Prompt appends a single trailing line `TONE: <0-3>`. Parse: extract `TONE:`, coerce, **clamp
  0-3**; on missing/unparseable, `tone = 0` and **still persist bullets**.
- **Strip the trailing `TONE:` line out of `bullets` before storing/rendering** (it renders at
  `summary.html:17`) - the one piece pass 2 flagged as not-yet-explicit.
- Update **every** caller and fake (re-derive exact lines at implement time - pass-2 noted the
  draft's numbers are stale): callers `main.py` (~66) and `web.py` (~376); the **two** fakes in
  `test_web.py` (~24 and ~32); `tests/llm/test_provider.py` failure test (~63) currently asserts
  `summary is None` and must assert the `SummaryResult` shape. Rely on mypy strict + a full
  `pytest` to catch the rest.

### Where it plugs in

- `thread.py`: `ReplyRecord`, `replies: list[ReplyRecord]`, `heated_tone: int = 0`;
  `reply_timestamps` becomes a derived projection.
- `slack/poller.py`, `slack/listener.py`: populate `replies` via `merge_replies` on all paths;
  listener gains author_id + text on the live event.
- `heat.py`: `structural_heat` / `is_heated` (render-time, like `is_zombie`), with debug logging of
  `heated_score`, structural term, tone term, threshold, and the fire decision (logging rule).
- `web._emojis`: **Phase 2 only** - swap `heat_tier == "hot"` for `is_heated(...)`. Render order
  unchanged (🔥 stays between ⚡ and 🧟).
- `provider.py`: `SummaryResult` + tone parse/strip.
- `base.html`: warm-color nudge (Phase 2). Legend (`index.html:15`) + tooltip +
  `slack-dashboard.example.yml:48` (still says fire = hot tier) → "🔥 heated exchange".

### Config (kebab-case, in `HeatConfig`)

```yaml
heated-threshold: 8.0              # heated_score at/above which 🔥 fires
heated-structural-scale: 1.0       # scales gated intensity into the 0-10 band
heated-tone-weight: 3.0            # 0-3 tone * 3.0 = 0-9, so max tone alone clears the threshold
```

Threshold calibrated **once, in Phase 2**, with both terms live.

## Additional change (unrelated to 🔥): message count, not response count

The row count column shows `Nr` ("N responses"), computed as `len(replies) - 1` (root excluded,
`poller.py:339`). Change it to `Nm` ("N messages") = the **total** message count including the
initial author's root message - drop the `-1`. This is independent of the 🔥 work but touches the
same files, so it rides along in Phase 1.

- **Rename** `ThreadEntry.reply_count` → `message_count` (a field named `reply_count` holding a
  message total is misleading). Update every consumer: `heat.py:64` (`base`), the size-tier label
  (`web.py:297`), the unanswered proxy (`web.py:161`), the title/summary watermarks (`poller.py`,
  `main.py`), and the incremental/listener increments (`poller.py:273`, `listener.py:79`) - those
  still `+= n` correctly; only the initial value loses its `-1`.
- `poller.py:339`: `reply_count=len(replies) - 1` → `message_count=len(replies)` (Slack's
  `conversations_replies` returns root + replies, so `len(replies)` is the total).
- **Templates:** `threads.html:21` and `channel.html:9` → `{{ row.message_count }}m`, tooltip
  "N message(s)". Update `CLAUDE.md` (`Nr → N responses`; `3r|3p` examples → `3m|3p`).
- **Bonus - resolves the count/participant off-by-one** this design otherwise worked around:
  `participants` already includes the root author, so once `message_count` includes the root too,
  both use a consistent denominator and the structural `back_and_forth` / floor needs no
  special-casing.
- **Threshold touch-ups (minor, calibrate-by-observation):** every thread's count rises by exactly
  1 - a near-uniform shift - so heat *ranking* is essentially unchanged. Two thresholds compare
  against the count and should be re-eyed: `hot_threshold` / `warm_threshold` (via `base`), and
  especially `unanswered_max_replies` (a no-reply question is now `message_count == 1`, not `0`).

## Phasing

- **Phase 1 - groundwork** *(model: sonnet)*: `ReplyRecord` + `replies` + `merge_replies`, wired
  into poller (full + incremental) and listener; `reply_timestamps` as a derived projection;
  `SummaryResult` and every caller/fake updated; `structural_heat` / `is_heated` with
  `tone_term = 0`; `heated-*` config + defaults; debug logging; **the `reply_count` →
  `message_count` rename + `Nm` display** (the unrelated change above - the only user-visible part
  of Phase 1). **🔥 still fires on `heat_tier == "hot"`** - no glyph/color/legend/prompt change.
  De-risks the data-model change and the return-type ripple independently; fully unit-testable,
  no LLM.
- **Phase 2 - the repurpose** *(model: opus)*: extend `generate_summary` to emit/parse/strip the
  0-3 `TONE:` line, store `heated_tone`, fold `tone_term` in; **flip `_emojis` to `is_heated`**;
  warm-color CSS; legend + tooltip + `example.yml`; calibrate `heated-threshold`. Rewrite the
  fire-behavior tests that assert the old `heat_tier == "hot"` glyph (`test_grouping.py:162`,
  `test_web.py:378-403`). Fake provider returns known tones.

## Alternatives Considered

- **Drop 🔥 entirely.** Rejected - a free, understood glyph slot is better spent on a real signal.
- **Ship structural-only 🔥 in Phase 1.** Rejected (finding 6): overlaps ⚡; hence groundwork-only.
- **Separate `reply_texts` + `reply_timestamps` lists.** Rejected (pass 2): two lists with two
  dedupe policies race on the three ingestion paths. One ordered `replies` record with one merge
  path is the fix.
- **Raw replies-per-person / raw velocity in `intensity`.** Rejected (pass 2): fires on monologues
  and reintroduces ⚡. Replaced by exchange-gated intensity.
- **Clamp after floored decay.** Rejected (pass 2): pins old fights at 10 forever. Clamp first,
  decay with no floor.
- **Tone via a separate LLM call / 0-10 scale.** Rejected: cost, and LLM inconsistency at 0-10.

## Resolved Decisions

1. **Warm color - Phase 2, mandatory.** Hot/warm contrast ~1.74:1; once 🔥 stops reinforcing
   "hot," text color is the only tier cue. Nudge warm toward clear yellow/amber, in the same phase
   as the flip so the visual semantics change atomically.
2. **Tone scale 0-3**, `heated-tone-weight: 3.0` to preserve tone's reach.
3. **Strong tone alone fires 🔥** (`3 * 3.0 = 9 >= 8`) - the quietly-hostile, low-volume thread is
   exactly what heat-ranking buries.

## Deferred

- Restart persistence of `replies` / `heated_tone` - `ThreadEntry` is in-memory; both rebuild on
  next fetch/summary. Acceptable for v1.
