# Design Document: Following, and the Knobs on the Live Board

**Author:** Scott Idler
**Date:** 2026-10-18
**Status:** Implemented
**Review Passes Completed:** 5/5
**Panel rounds:** 1 (synthesis `~/.cache/review-panel/runs/Z1HWX652/synthesis.md`, every finding folded in or dispositioned below)

## Summary

Two things, both from the same afternoon of reading the deployed board.

1. **Following.** Slack already tells me about every thread I follow: one I started, one I
   commented in, one I was @'d in, one I clicked "Get notified about new replies" on. The
   board's job is the other threads. Today the damping only knows "I posted" and climbs
   back toward neutral as replies arrive, which is backwards. Slack's own `subscribed`
   flag on the thread root becomes the signal, a followed thread takes a flat drop, and
   the 🙋 slot becomes 🔔. The tuning harness's purity term changes with it, because it
   was crediting the threads Slack already covers.
2. **The knobs on the site.** The explorer's knob panel (`bin/explore.html`, published on
   marquee) lets me drag a knob and watch the board reorder. The live site should do the
   same against the current data: every heat knob behind a toggle, defaults from the yml,
   overrides in the request and the query string, never written back. One formula serves
   both: the server re-ranks with the overridden config and re-renders the same partials.

## Problem Statement

### Background

- The board heat-ranks threads from `~/.config/slack-dashboard/slack-dashboard.yml`'s
  channels. The formula (`src/slack_dashboard/heat.py`) was tuned by the autoresearch loop
  (`docs/design/2026-10-18-heat-autoresearch.md`); the tuned values live in the yml and
  the code defaults stay neutral so the frozen arena (`tests/calibration/`) holds.
- Involvement today: `is_involved` (`heat.py:466`) is "my user id is in `participants`".
  `involvement_damping` (`heat.py:109`) cuts the score to `involved_drop` (yml 0.8) the
  moment I post and rebuilds it by `involved_rebuild_per_msg` (0.15) per reply after my
  last post, so about seven unseen replies restore the thread to neutral.
- The refresh loop is incremental: `_fetch_channel` passes the channel watermark as
  `oldest` (`poller.py:454`), and the reconnect reconcile re-fetches a thread only when
  its `latest_reply` moved (`poller.py:200-215`). A thread with no new reply is never
  re-read. The listener handles message events only.
- The explorer (`bin/explore.html`) renders the knobs as sliders over a four-week corpus
  and scores in its own JavaScript copy of the formula. It needs labels and a corpus, so
  it lives on marquee, not in the app.
- The harness objective is `mean(catch, purity)`. Purity credits a top-10 row when the
  thread is one I *ever* joined (`bin/replay.py:281`, `e["joined"]`), including looks
  after my first reply, when Slack is already following it for me.
- Spike, run 2026-10-18 with the app's user token against the deployed config:
  - `conversations.replies` returns the root first, carrying `subscribed` and `last_read`,
    on a full fetch and on an incremental fetch with `oldest` set (the root is still the
    first message, still carrying the flag).
  - `conversations.history` parents carry `subscribed` and `last_read` too.
  - On the thread I started (`C039YLDJW5T/1792195826.029839`): `subscribed: true`,
    `last_read` present. On a thread I am not in (`C039YLDJW5T/1791501819.842559`):
    `subscribed: false`, key present, no `last_read`.
  - The Slack MCP's token does not surface the field; the app's user token does.
- Measured on main for this doc (2026-10-18, `bin/replay.py`, org weights, best formula):

  | formula | purity term | train | holdout |
  |---|---|---|---|
  | best (drop 0.8, rebuild 0.15) | ever joined (today) | 0.5878 | 0.5969 |
  | drop 0.2, rebuild 0 | ever joined (today) | 0.5529 | 0.5472 |
  | best (drop 0.8, rebuild 0.15) | needed at the look | 0.4956 | 0.4536 |
  | drop 0.2, rebuild 0 | needed at the look | 0.5167 | 0.4814 |
  | live yml (drop 0.2, rebuild 0), committed harness at 4a5fb49 (post-change) | needed at the look | 0.5167 | 0.4814 |

  Under today's purity a hard followed drop loses 3 points, because purity pays for
  showing threads I was already in. Under purity that credits only rows needed at the
  look, the same drop gains 2 on train and 3 on holdout.
- The arena's `involvement_drop_then_rebuild` (`tests/calibration/criteria.py:183`)
  passes at `involved_rebuild_per_msg` 0.15 and at 0.0 (run for this doc). Its
  `_rebuild_variant` adds a participant along with the unseen replies, so the score rises
  from the participant term, not the rebuild. The criterion does not test the rebuild.
- `_KebabModel` (`config.py:19`) has no `extra="forbid"`. `HeatConfig` accepts an unknown
  key, `base_cap=nan`, an `inf` channel weight and `reply_weight=-1` (run for this doc).
  Old involvement keys are warned about and ignored (`config.py:248-270`).

### Problem

- A thread I started with a question for my team sits at rank 1 with 👔 lit next to 🙋.
  Slack is already going to tell me when it moves. The board is spending its top row on
  something I cannot miss, and the rebuild makes it climb as the thread grows.
- "I posted" misses two of the four ways Slack follows a thread for me: an @-mention and
  a manual follow. Those threads rank as if I had never seen them.
- The harness rewards the opposite of the rule: its purity term pays for keeping followed
  threads on the board.
- The explorer's knobs cannot be tried against today's board. Every "what if the
  half-life were 1h" is a yml edit and a container restart, or a trip to the marquee page
  with last month's data.

### Goals

- A followed thread (Slack's definition) ranks much lower than an unfollowed one, by a
  yml knob, with no rebuild in my config. (Scott, 2026-10-18: "a thread I start or am
  already in should rank much lower than items happening in threads that I have not seen
  and not started watching.")
- No blanket rule on the marks: 👔 stays honest on a followed thread. (Scott: "I could
  start a thread and then issues arose and I may be needed.")
- A follow or unfollow in Slack reaches the board within one refresh interval without a
  new reply.
- The harness agrees with the rule: purity credits only rows needed at the look, and the
  following knobs are owner policy the loop cannot tune.
- Every explorer knob and widget that works on live data is on the site behind a toggle,
  loaded from the yml, changeable in the browser, re-ranking the current data. (Scott:
  "all of these widgets and knobs should be made available on our published website.
  probably hidden behind a toggle.")
- One formula. The site never grows a JavaScript copy of `heat.py`.
- The yml stays the source of truth. Nothing on the site writes to it.
- Overrides are user input: validated, bounded, fail-loud.

### Non-Goals

- **Excluded**: writing knob changes back to the yml from the browser. Config loads once
  at boot, there is no hot reload, and a web-to-config path is a mutation channel the
  site should not have. A keeper is copied as yml and pasted by hand.
- **Excluded**: the sweep, the objective tiles, the label pickers, the bloomer knobs. They
  need a labeled corpus; the live app has none. They stay in the explorer.
- **Excluded**: per-viewer persistence on the server. Knob state is in the query string.
- **Excluded**: the mark knobs (`spiking-threshold`, `heated-*`, `new-window-minutes`,
  `unanswered-*`, `resurrection-*`). The explorer has no such knobs; nothing was asked.
- **Parked, revisit when `events.jsonl` has a few weeks of opens**: `last_read` as a
  read-or-not signal for the next tuning label.

## Proposed Solution

### Overview

- **Following**: `ThreadEntry` gains `subscribed` and `last_read`, read from the root on
  every thread fetch and from every channel parent on every refresh (the metadata sweep),
  and set by the listener the moment one of my own messages lands. Damping keys on
  `subscribed`. The knobs are renamed to say what they gate: `followed-drop`,
  `followed-rebuild-per-msg`. The rebuild counts retained replies after `last_read`
  (unread in Slack). My yml: drop 0.2, rebuild 0.
- **Harness**: purity credits a top-10 row only when it is needed at that look
  (`before_me`); `followed_drop` and `followed_rebuild_per_msg` are fixed by the evaluator
  the way board membership already is (`replay.py:314`).
- **Knobs**: `/threads`, `/summarize` and `/channel` accept `heat.<key>` query parameters.
  Each route merges them over the yml's `heat` block, re-validates through a hardened
  `HeatConfig`, scores with a pure function that never writes into the shared entries, and
  renders the same partial. The panel is a server-rendered partial listing every knob with
  its yml value; JavaScript keeps the overrides in a map that rides `hx-vals` on every
  request and mirrors into the URL. The shaping charts come along on live data.

### Architecture

```
Slack thread root ──subscribed, last_read──▶ poller._fetch_thread ─────▶ ThreadEntry
Slack channel parents (every refresh, oldest=None) ──▶ poller._sweep_metadata ──┘  ▲
listener own-post ──subscribed=True, last_read≈ts (estimate until swept) ──────────┘
                                                      heat.following_damping(entry, cfg, now)

GET /threads | /summarize | /channel  ?…&heat.velocity-weight=30&heat.channel-weights.sre-internal=2
   │
   ├─ overrides = knobs.parse_overrides(query_params)                 unknown key -> ValueError
   ├─ cfg       = knobs.apply_overrides(config, overrides)            HeatConfig re-validated, 422 on error
   ├─ ranked    = heat.score_threads(eligible(entries), cfg.heat, now) pure: list[Ranked]
   └─ render the partial; the response embeds the overrides it was rendered with

poller.ranked_threads()  ──▶ score_threads(eligible(entries), yml.heat, now) then writes
                             heat_score / heat_tier back (events, the poller's own uses)
```

- `score_threads` is the one scoring path. `eligible` (dismissed filtered, stale filtered)
  is a pure helper both callers share, so the override path reproduces `ranked_threads`'
  membership without calling the mutating wrapper (`poller.py:104`).
- The poller calls `score_threads` with the yml config and writes the result back into the
  entries, as today. A route calls it with the request's config and never writes back, so
  a viewer's override cannot leak into the event log, the poller, or another viewer.
- Overrides apply to the three read routes alike, through one dependency. The hover strip
  is the only place the score is printed; it explains the ranking the viewer is looking
  at.
- At one replica this is a request-scoped config object. At N replicas it is the same
  request-scoped config object. Nothing is shared.

### Data Model

`ThreadEntry` (`src/slack_dashboard/thread.py`):

| field | type | source | meaning |
|---|---|---|---|
| `subscribed` | `bool = False` | root `subscribed` (thread fetch, metadata sweep); listener own-post | Slack follows this thread for me |
| `last_read` | `float \| None = None` | root `last_read`; listener own-post (estimate) | ts of the last message I read in Slack; `None` = unknown |

- The root is the message whose `ts == thread_ts`, never "the first item": with `oldest`
  set the client concatenates pages (`client.py:123`).
- A root without the `subscribed` key leaves both fields as they were and logs a warning
  with the channel and thread (the key was present on every root in Phase 0).
- The listener's `last_read` is an estimate (my own post's ts); the next sweep replaces it
  with Slack's value.

`HeatConfig` (`src/slack_dashboard/config.py`), renamed, defaults unchanged:

| old key | new key | default | my yml |
|---|---|---|---|
| `involved-drop` | `followed-drop` | 0.8 | 0.2 |
| `involved-rebuild-per-msg` | `followed-rebuild-per-msg` | 0.15 | 0 |

Hardening, applied to every config model (`_KebabModel`), because override values are
user input and the yml deserves the same:

- `extra="forbid"`: an unknown key is an error, in the yml and in a query. The old
  involvement keys (`involved-*`, and the three older ones `config.py:248` warns about)
  are errors with the replacement named, not warnings that run defaults.
- `allow_inf_nan=False` on every float field.
- Non-negative where the formula needs it (weights, caps, counts, hours); the existing
  `> 0` denominators stay.
- Bounds on the override path only: `max-thread-age-days` is capped at the yml's value
  (the poller evicts past it, so a larger value cannot bring threads back); an override
  above it is a 422, not a silent clamp.

`display.marks`: slot key `me` becomes `following`, default 🔔. An unknown key fails at
boot (`extra="forbid"` makes the rename loud too).

`events.jsonl`: an `open` or `dismiss` made from a surface rendered with overrides carries
`"overrides": {"heat.velocity-weight": "30", ...}`, the map the surface was rendered with.
Rows with `overrides` are excluded from the next tuning label, not re-scored: the event
records the yml rank from `_record_event` (`poller.py:134`) and has no board snapshot.

### API Design

**Overrides** (`knobs.py`, one FastAPI dependency used by `/threads`, `/summarize`,
`/channel`):

- `heat.<kebab-key>=<value>` for a scalar `HeatConfig` field, e.g. `heat.velocity-weight=30`,
  `heat.atrophy-curve=hyperbolic`.
- `heat.channel-weights.<channel>=<float>` and `heat.people-weights.<user-id>=<float>` for
  the two maps. A name absent from the yml map is added; one present is replaced.
- A key given twice: the last value wins (`query_params.multi_items()` order).
- Unknown key, unknown field, a value `HeatConfig` rejects, or `max-thread-age-days` above
  the yml: `422` with the validator's message as the body. Nothing is clamped.
- No overrides: byte-identical to today's output.
- The rendered partial carries the overrides it was rendered with
  (`data-overrides='{"heat.velocity-weight":"30"}'` on the partial's root element); the
  open beacon and the dismiss request read that attribute, not the global map, so an
  event names the board that was clicked even if a later request or a 422 changed the
  map.

**422 under HTMX**: HTMX does not swap a 4xx response. An `htmx:responseError` listener on
`#thread-list` handles only a response whose `requestPath` begins with `/threads`
(hover, popover and dismiss requests also originate inside the list and must not touch the
board's error); it sets `#knob-error`'s `textContent` to the body. A successful `/threads`
response clears it. A bad value pasted into the URL on first load follows the same path:
the "Loading" placeholder stays and the error shows above it.

**`GET /knobs`** (new): the panel partial. Server-rendered from `knobs.KNOBS` with every
knob's yml value, plus the channel and people maps from the yml. Loaded once on page load
(`hx-trigger="load"`). The topic knobs render dimmed with "no rater configured" when
`topics.api-key` is empty (`main.py:85`), since no thread carries a probability then.
After the partial loads, the overrides map seeded from the URL is applied to the inputs
(hydration happens after `/knobs`, not before).

**`knobs.py`** (new module): one table, the only list of what the panel shows.

```python
@dataclass(frozen=True)
class Knob:
    key: str          # HeatConfig field name (snake); kebab in the query and the panel
    group: str        # size | time | burst | following | topics | tiers
    label: str
    lo: float
    hi: float
    step: float
    help: str
    choices: tuple[str, ...] = ()   # atrophy_curve, tier_method
    narrows_only: bool = False      # max_thread_age_days
```

Knobs (all `heat.` keys): `reply-weight`, `participant-weight`, `people-weight-cap`,
`base-cap`, `base-k`; `atrophy-half-life-work-hours`, `atrophy-curve`,
`atrophy-leader-stretch`, `alive-weight`, `alive-k`, `max-thread-age-days` (narrows only);
`velocity-weight`, `velocity-window-minutes`, `activity-cap`; `followed-drop`,
`followed-rebuild-per-msg`; `leader-weight`, `leader-gate-floor`, `resolved-drop`;
`tier-method`, `tier-hot-count`, `tier-warm-count`, `tier-floor`, `tier-hot`, `tier-warm`.
Then `channel-weights` (one slider per channel in the yml) and `people-weights` (one per
id in the yml). Ranges copy the explorer's, corrected where the validators disagree:
`reply-weight` and `participant-weight` are ints (step 1); `atrophy-leader-stretch` tops
out at 1.75 (`HeatConfig` rejects 2); `activity-cap` starts at 1 (the explorer's 0 is a
divide-by-zero the validator rejects, `config.py:318`).

Coverage test: every `HeatConfig` field, whatever its type, is either a `Knob.key` or in
`NOT_A_VIEW_KNOB` with a reason (`work_window`: a nested model; `decay_hours`,
`decay_floor`, `hot_threshold`, `warm_threshold`: superseded by atrophy and tiering and
kept for config compatibility; `retitle_*`: the LLM retitle rule; the mark knobs: not a
rank input; `channel_weights`, `people_weights`: rendered as maps). A stale name on either
side fails. A second test round-trips every knob's `lo` and `hi` through `HeatConfig`, so
the slider can never offer a value the server rejects.

**Panel behaviour** (`partials/knobs.html` + JavaScript in `index.html`):

- A `knobs` toggle beside the group-by control. Closed by default, every load.
- Each knob: label, range input, number input, the yml value as a tick. A changed knob is
  highlighted; `reset` per knob and `reset all`.
- Slider input is debounced (150 ms) before the request; a request in flight is not
  cancelled, the next one carries the latest map.
- `window.slackDashKnobs` holds `{ "heat.velocity-weight": "30", ... }`. `hx-vals` on
  `#thread-list` spreads it in beside `group-by` and `compact`; the hover and popover
  requests include it through `hx-include` of a hidden form the map is mirrored into.
  `history.replaceState` mirrors it into `location.search`; on load the map is seeded from
  `location.search`, so a reload or a pasted link reproduces the board.
- `copy as yml` renders the changed knobs as a `heat:` block (maps nested) into a `<pre>`
  and the clipboard. That is the whole write path. (Author addition: the panel needs some
  way to land a keeper, and this is the one that keeps the yml the source of truth.)

**Shaping charts** (Phase 4): `GET /knobs/inputs` returns, per eligible thread, the raw
inputs the explorer's charts bin (volume, working hours since last, p(leader), the score
under the request's overrides). The explorer's `chart()` (bars of the input's distribution
on the current board, the knob's curve on top, the two axis ticks) is moved into a shared
static file both pages load, so the chart code exists once.

### Implementation Plan

#### Phase 0: Prove the flag with zero code
**Model:** sonnet
- Done 2026-10-18, results in Background above. Re-run before Phase 1 if the token or the
  workspace changes:
  `curl -s -H "Authorization: Bearer $SLACK_DASHBOARD_SLACK_USER_TOKEN" "https://slack.com/api/conversations.replies?channel=C039YLDJW5T&ts=1792195826.029839&limit=1"`
  and read `messages[0].subscribed`.
- Still to probe in the same shape before Phase 1 writes the incremental path: a replies
  call whose `oldest` is newer than every reply (expect the root alone), and a channel
  history page beyond the first (expect `subscribed` on every parent).
  Observed 2026-10-18 (before Phase 1): replies on `C039YLDJW5T/1792195826.029839` with
  `oldest=1792197235.200000` (past the last reply `1792197235.109269`) returned 1 message,
  the root, `subscribed: true`, `last_read` present. History page 2 of `C039YLDJW5T`
  (limit 50): 39 parents with replies, all 39 carry `subscribed` (26 true); the 11
  messages without replies carry no `subscribed` key.
- **Success criteria:** `subscribed` present on the root in `conversations.replies` (with
  and without `oldest`) and on `conversations.history` parents; true on a thread I
  started, false on one I am not in. Observed 2026-10-18: all four.

#### Phase 1: Following
**Model:** opus
- `ThreadEntry.subscribed`, `ThreadEntry.last_read`. `_fetch_thread` reads both from the
  message whose `ts == thread_ts` on the full and the incremental path; a missing key
  logs a warning and leaves the fields.
- Metadata sweep: `_refresh_loop` adds `_sweep_metadata`, one `fetch_threads(oldest=None)`
  per channel per interval, updating `subscribed` and `last_read` on every entry whose
  parent comes back, without fetching replies. One request per channel, paced by the
  client (`client.py:35`), so a sweep of C channels costs about 1.2C seconds against a
  10-minute interval. Max staleness of a follow or unfollow made in Slack: one
  `refresh-interval-minutes`.
- Listener `_apply_event`: when `event["user"]` is me, set `subscribed = True` and
  `last_read = ts` (estimate). `SocketListener` gains a zero-argument `self_user_id`
  callable, wired in `main.py:125`, because the poller resolves the id in `start()` after
  the listener is built (`poller.py:227`).
- `heat.py`: `is_involved` becomes `is_following(thread) = thread.subscribed`.
  `involvement_damping` becomes `following_damping`: off when not following or
  `followed_drop >= 1`; rebuild counts retained non-root replies with `ts > last_read`,
  and counts none when `last_read` is `None` (unknown read state is not "unread
  everything"); clamp as today. `self_user_id` leaves the heat signatures
  (`heat_breakdown`, `compute_heat`, `rank_threads`); the poller keeps it for the listener
  and the `following` mark reads `thread.subscribed`.
- Rename `involved_drop` / `involved_rebuild_per_msg` to `followed_drop` /
  `followed_rebuild_per_msg` everywhere: `HeatConfig`, the example yml, the live yml,
  `web.py` (`me` -> `following`), `config.MARK_STATES`, `DEFAULT_MARKS` (`following: 🔔`),
  `_FLAG_HELP`, `bin/explore.py:308`, `bin/calibrate.py:81-100`,
  `tests/calibration/score.py:147`, `bin/formula.py`, `bin/explore.html`, and
  `~/.local/share/slack-dashboard/formula-best.py` (copied forward after the rename).
- Config hardening: `extra="forbid"`, `allow_inf_nan=False`, non-negative validators; the
  old keys become errors naming the new key.
- Arena (`tests/calibration/`): `board.py`'s `involved=` flag also sets `subscribed=True`
  and `last_read` to the self post's ts. `criteria.py`: every `board.SELF_ID` call site
  (10 lines) drops the id argument; `involvement_drop_then_rebuild` compares a
  `subscribed=True` copy against a `subscribed=False` copy, and `_rebuild_variant` holds
  participants, message count and timing constant and varies only `last_read`, so the
  rebuild half of the predicate tests the rebuild. A new arena test asserts the predicate
  is False at `followed_rebuild_per_msg = 0` (the criterion bites). The seven criterion
  names stay; the defaults are unchanged so the arena passes without a value change.
- Harness: `bin/replay.py` purity counts `before_me` rows; `Features.i_posted` ->
  `following` (started or posted before the look: the corpus has no subscription
  history, mentions and manual follows are the gap); the evaluator fixes
  `followed_drop` and `followed_rebuild_per_msg` from the yml the way it fixes membership
  (`replay.py:314`). `bin/program.md`: the purity definition, the fixed knobs, and the
  taste rule that called a hard involved drop "the label's blind spot" is replaced by
  "followed threads drop by design; the loop cannot move those knobs".
- Replay after the harness change, recorded here: expected train 0.5167 / holdout 0.4814
  for drop 0.2 / rebuild 0 (the scratch measurement above; the committed run is the
  record).
- Live yml: `followed-drop: 0.2`, `followed-rebuild-per-msg: 0`, `display.marks.following`.
- Tests: poller reads the flag on both fetch paths and through a rebuild; the sweep flips
  `subscribed` and `last_read` on a thread with no new reply; listener own-post sets
  both; damping off when not following; flat at drop with rebuild 0; rebuild counts only
  retained non-root replies after `last_read` and none when it is `None`; mark
  `following` lit from `subscribed` alone; config rejects the old keys, an unknown key,
  `nan`, `inf`, and a negative weight.
- **Success criteria:**
  - `uv run pytest tests/slack/test_poller.py -q -k subscribed` passes and collects a test named `test_unfollow_without_new_reply_reaches_the_board`.
  - `uv run pytest tests/calibration -q` passes, collects `test_involvement_criterion_fails_at_rebuild_zero`, and `uv run python -c "from tests.calibration import criteria; print(' '.join(n for n, _ in criteria.CRITERIA))"` prints the same seven names as on main.
  - `rg -c "involved_drop" src bin tests` returns no files.
  - `uv run python -c "from slack_dashboard.config import HeatConfig; HeatConfig(bogus=1)"` exits non-zero.

#### Phase 2: Pure scoring and request overrides
**Model:** opus
- `heat.score_threads(threads, config, now) -> list[Ranked]`, `Ranked(entry, score, tier)`;
  `poller.eligible()` as the shared membership helper. `rank_threads` and
  `poller.ranked_threads` call them and write back, as today. `web.group_threads` /
  `_build_row` take `Ranked` and the request's config, so the row's tier, the strip's
  colour and the `Nh` breakdown come from the request's ranking, not from the entry.
- `knobs.py`: `KNOBS`, `NOT_A_VIEW_KNOB`, `parse_overrides`, `apply_overrides`, and the
  FastAPI dependency `request_config` used by `/threads`, `/summarize`, `/channel`.
- The three routes: build the config through the dependency, 422 on `ValueError` /
  `ValidationError` with the message; embed `data-overrides` on the partial's root.
- `poller.open_thread` and `dismiss_thread` take an optional overrides map; the open
  beacon and the dismiss request send the rendered surface's `data-overrides`;
  `_record_event` writes it as `overrides` when present.
- Tests (`tests/test_knobs.py`), with time frozen: an override that changes scores and
  tiers reorders the board and `poller.threads[...]` keep their `heat_score` and
  `heat_tier`; a following request without overrides returns the yml order and tiers;
  dismissed and stale threads stay out under overrides; unknown key, `nan`, a negative,
  and `max-thread-age-days` above the yml are 422 with the validator's text;
  `channel-weights.x` adds and replaces; `/summarize` with an override prints the
  overridden score; no overrides is byte-identical to today's render; an open and a
  dismiss with `data-overrides` carry `overrides` on the event line and ones without have
  no such key.
- **Success criteria:**
  - `curl -s -o /dev/null -w "%{http_code}" "http://localhost:8889/threads?heat.velocity-weight=abc"` returns `422`.
  - `curl -s "http://localhost:8889/summarize/<C>/<ts>?heat.velocity-weight=60" | md5sum` differs from the same URL without the parameter on a thread with replies in the window.
  - `uv run pytest tests/test_knobs.py -q` passes and collects `test_override_never_writes_into_shared_entries`.

#### Phase 3: The panel
**Model:** sonnet
- `GET /knobs` and `partials/knobs.html` from `KNOBS`, grouped as the explorer groups
  them, with the yml value per knob, the two weight maps, and the topic group dimmed when
  no rater is configured.
- `index.html`: the toggle, the overrides map, `hx-vals` spread and the hidden form for
  `hx-include`, the debounce, `replaceState`, seeding from `location.search` and
  hydration after `/knobs` loads, per-knob and global reset, `copy as yml`, the scoped
  `htmx:responseError` listener.
- `base.html`: panel styles. Dense, same rules as the rows: no cards, no padding.
- Tests: `/knobs` lists every `KNOBS` key with its yml value; a yml value shows as the
  tick; the kebab key in the panel equals the query key `parse_overrides` accepts (one
  round trip per knob); the topic group carries the dimmed marker when the key is empty.
- Docs: README "UI" and CLAUDE.md "Design principle" paragraphs; the example yml gains
  nothing (no new keys); `docs/design/2026-10-18-heat-autoresearch.md` "Next" gains the
  panel and the purity change.
- **Success criteria:**
  - `curl -s http://localhost:8889/knobs | rg -c 'data-knob="heat\.'` equals `len(KNOBS)` plus the number of yml channel and people weights.
  - Shakedown on the deployed board, recorded in the implementation notes: drag `velocity-weight` to 60, the URL carries it and the order changes; reload, same order; `copy as yml` pasted into a scratch yml loads with `load_config`; a bad value typed into the URL shows the 422 text above the board and the board keeps its last render.

#### Phase 4: Shaping charts on live data
**Model:** sonnet
- `GET /knobs/inputs` (JSON, under the same overrides dependency); the explorer's
  `chart()` moved to a shared static file; four charts under the panel (volume -> base,
  hours since last -> atrophy, p(leader) -> vote, score distribution), redrawn on every
  `/threads` response.
- Tests: `/knobs/inputs` returns one row per eligible thread with the overridden score;
  the shared chart file is the one `bin/explore.html` loads (a test greps both for the
  same path).
- **Success criteria:**
  - `curl -s "http://localhost:8889/knobs/inputs" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))"` equals the number of rows `/threads` renders.
  - `rg -c "function chart\(" bin/explore.html src/slack_dashboard/static/` shows the function defined in exactly one file.

## Acceptance Criteria

- [ ] `uv run pytest tests/slack/test_poller.py -q -k subscribed` passes and collects `test_unfollow_without_new_reply_reaches_the_board`.
  Observed on main (d9bd892): `28 deselected in 0.38s`, nothing collected.
- [ ] `rg -c "involved_drop" src bin tests` returns no files.
  Observed on main (d9bd892): `src/slack_dashboard/heat.py:11`, `src/slack_dashboard/config.py:3`, `bin/formula.py:3`, `bin/explore.html:4`.
- [ ] `uv run pytest tests/calibration -q` passes, collects `test_involvement_criterion_fails_at_rebuild_zero`, and the seven criterion names are unchanged.
  Observed on main (d9bd892): passes (416 tests in `otto ci`); `involvement_drop_then_rebuild` returns True at rebuild 0.15 and at 0.0; names `at_most_N_red lunchtime_threads_demoted active_recent_top3 stale_is_cold weekend_frozen involvement_drop_then_rebuild vip_lift_capped`.
- [ ] `uv run python -c "from slack_dashboard.config import HeatConfig; HeatConfig(bogus=1)"` exits non-zero.
  Observed on main (d9bd892): accepted (exit 0), likewise `base_cap=nan`, an `inf` channel weight, `reply_weight=-1`.
- [ ] `curl -s -o /dev/null -w "%{http_code}" "http://localhost:8889/threads?heat.velocity-weight=abc"` prints `422`.
  Observed on main (d9bd892): `200`.
- [ ] `/summarize/<C>/<ts>?heat.velocity-weight=60` renders a different strip than without the parameter, on a thread with replies in the window.
  Observed on main (d9bd892): identical (`cf8fc073` both) on `C039YLDJW5T/1792195826.029839`.
- [ ] `curl -s -o /dev/null -w "%{http_code}" http://localhost:8889/knobs` prints `200` and the panel lists every knob.
  Observed on main (d9bd892): `404`.
- [ ] `uv run bin/replay.py` on the committed harness prints `objective:` and `holdout:` for the live yml knobs, recorded in this doc's Background table as the post-change row.
  Observed on main (d9bd892): `0.5878 / 0.5969` under the old purity; `0.5167 / 0.4814` for drop 0.2 / rebuild 0 under the new purity (scratch run).

## Resolved Decisions

- **2026-10-18, Scott**: no blanket "👔 off when 🙋 on" rule. The mark stays honest; the
  rank drops instead.
- **2026-10-18, Scott**: following is Slack's definition (started, commented, @'d, manual
  follow), and a followed thread ranks much lower. The board is for the threads Slack
  will not tell me about.
- **2026-10-18, Scott**: "all of these widgets and knobs" on the site. The charts are
  widgets: Phase 4 is in the plan, not parked.
- **2026-10-18, panel round 1, author**: the follow state needs its own refresh. The
  incremental fetch never re-reads a quiet thread, so the metadata sweep (one channel
  history call per channel per interval) is the delivery mechanism, with a test that
  flips the flag on a thread with no new reply.
- **2026-10-18, panel round 1, author**: the rebuild knob stays with its default, keyed on
  `last_read`, 0 in my yml. The arena criterion is repaired to test the rebuild (it did
  not), and a test proves it fails at 0. Architect preferred removal; the knob is one
  line and the repaired criterion keeps the frozen arena meaningful.
- **2026-10-18, panel round 1, author**: the harness purity term changes to rows needed
  at the look. The old term paid for showing threads Slack already covers and so rewarded
  the opposite of the rule (measured: the followed drop lost 3 points under it and gains
  2 to 3 under the new one). The following knobs are fixed by the evaluator, not by prose.
- **2026-10-18, panel round 1, author**: overrides apply to `/summarize` and `/channel` as
  well as `/threads`, through one dependency. Both seats: the strip is where the score is
  printed, so it has to print the viewer's ranking.
- **2026-10-18, panel round 1, author**: every config model forbids unknown keys and
  rejects `nan`/`inf`; old keys are errors. Overrides are user input; the yml gets the same
  treatment because silent defaults on a renamed key is the failure mode the warning in
  `config.py:248` only half-closed.
- **2026-10-18, panel round 1, author**: events carry the overrides of the rendered
  surface, on opens and dismisses, and the next label excludes those rows.
- **2026-10-18, panel round 1, author**: an unfollow in Slack is an unfollow here, within
  one refresh interval.
- **2026-10-18, panel round 1, author**: the panel's open state is not persisted (an
  unrequested convenience, dropped). `copy as yml` stays as the one write path; it is an
  author addition, flagged to Scott in the hand-back.

## Alternatives Considered

### Alternative 1: Keep "I posted", cut harder, drop the rebuild
- **Description:** no Slack change; `involved_drop` 0.2, rebuild 0 in the yml.
- **Pros:** a yml edit.
- **Cons:** misses @-mentions and manual follows, exactly the threads Slack already covers.
- **Why not chosen:** the flag is Slack's own definition of what it will tell me about,
  and it costs nothing to read (Phase 0).

### Alternative 2: Per-thread root re-fetch for the follow state
- **Description:** on every refresh, one `conversations.replies` call per board thread
  with `oldest` past the last reply, reading the root alone.
- **Pros:** no channel-level call.
- **Cons:** one paced request per thread (about 1.2 s each): two minutes per sweep on a
  hundred-thread board, growing with the board.
- **Why not chosen:** the channel history parents carry the same fields (Phase 0), one
  call per channel.

### Alternative 3: Score in the browser like the explorer
- **Description:** ship the thread inputs as JSON and port the explorer's JavaScript
  formula; knobs re-rank client-side with no round trip.
- **Pros:** instant; the chart code comes for free.
- **Cons:** two formulas. Every keep in `heat.py` has to be ported by hand, and the first
  drift makes the panel lie.
- **Why not chosen:** one formula is the point. A round trip on a 30s-polled board is
  not a cost. The charts still share their drawing code (Phase 4), which is presentation,
  not scoring.

### Alternative 4: Persist knob state on the server per viewer
- **Description:** a small store keyed by Authelia user.
- **Pros:** survives a new browser.
- **Cons:** state to migrate, state that differs by replica, a second config surface.
- **Why not chosen:** the query string already survives reload and share, and the only
  viewer is me.

### Alternative 5: Write a kept knob into the yml from the panel
- **Description:** a "save" button that edits the config file.
- **Why not chosen:** Non-Goals. Config loads at boot, there is no reload, and a
  web-to-config path is a mutation channel. `copy as yml` is the whole write path.

### Alternative 6: Keep purity as "ever joined" and only fix the knobs in the evaluator
- **Description:** freeze the following knobs, leave the objective alone.
- **Cons:** the objective would still pay for showing followed threads through every
  other knob (a longer half-life, a bigger vote), so the loop would keep drifting toward
  the threads Slack covers.
- **Why not chosen:** the harness has to agree with the rule it is tuning for.

## Technical Considerations

### Dependencies
- Slack: `subscribed` and `last_read` on root messages and on channel history parents,
  user token only (Phase 0).
- No new packages. FastAPI query parsing and dependencies, pydantic re-validation, HTMX
  `hx-vals` / `hx-include`.

### Performance
- Scoring is already per request (`ranked_threads` on every poll). The override path adds
  one `HeatConfig` construction per request and the same scoring. No jev calls, no Slack
  calls.
- The metadata sweep is one paced history call per channel per refresh interval (about
  1.2 s each, `client.py:35`), inside the existing refresh budget.

### Security
- The site is behind Authelia. Overrides are read-only against in-memory data, validated
  by the same hardened models as the yml, bounded above by the yml where the poller's
  state makes a larger value meaningless, never persisted, never written anywhere. A
  malformed query is a 422, not a clamped board.
- `last_read` and `subscribed` are my own read state; the board already shows my own
  participation.

### Testing Strategy
- Unit tests per phase as listed; the arena criterion repaired and proven to bite; the
  pure-scoring test with frozen time and both shared-state and baseline-order assertions.
- Shakedown after Phase 3 on the deployed board (Phase 3 success criteria), recorded in
  the implementation notes.

### Rollout Plan
- Each phase: one commit, `otto ci` green, `bump release` (ungated), `docker compose up
  -d --build` on ripr, probe the live board.
- Phase 1 ships with the live yml edit (renamed keys, new values, the mark key). Before
  the restart, the new yml is preflighted against the new image
  (`docker compose run --rm slack-dashboard python -c "from slack_dashboard.config import load_config, require_secrets; ..."`),
  because the app now refuses the old keys. Rollback is the previous image and the
  previous yml together; a `.bak` of the yml is kept beside it until the probe passes.
- Cross-repo blast radius: none. The explorer page on marquee is regenerated from
  `bin/explore.html` after Phase 1 and again after Phase 4 (`marquee replace`), so its
  knob names and its chart code match.

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Slack stops sending `subscribed` on a path | Low | Followed threads keep their last known state | Phase 0 is re-runnable; the poller warns per root without the key; the sweep re-reads every interval |
| A stale `subscribed=True` suppresses a thread | Low | A thread I unfollowed stays dropped | Bounded by one refresh interval; the sweep is the mechanism, with a test that removing it fails |
| A followed thread I need sinks past the fold | Med | I miss it on the board, Slack still notifies | That is the design; `followed-drop` is a knob and the panel makes trying 0.4 a drag |
| Overrides leak into shared entries | Low | Event log and poller show a viewer's numbers | `score_threads` is pure; the test asserts entries and baseline order are unchanged after an override request |
| Panel and model drift (new knob not in the panel) | Med | A knob is unreachable from the site | The all-fields partition test and the range round-trip test |
| Opens logged under overrides look like yml-rank opens | Med | The next tuning label learns from a board that never existed | The event carries the rendered surface's `overrides`; the label excludes those rows |
| The yml rename bricks the boot | Med | Board down until the yml is fixed | Preflight before restart; image and yml roll back together |
| A long query string | Low | Ugly URL | Only changed knobs are in the URL |

## Open Questions

- [ ] None. Every round-1 finding is folded in or dispositioned in Resolved Decisions.

## References

- `docs/design/2026-10-18-heat-autoresearch.md` (the tuned formula, the events log)
- `docs/design/2026-06-30-heat-remodel-and-calibration-arena.md` (the frozen arena)
- `bin/explore.html` (the knob panel and charts this ports), `bin/program.md` (the rules this amends)
- Panel round 1: `~/.cache/review-panel/runs/Z1HWX652/synthesis.md`
- Slack `conversations.replies`, `conversations.history`: `subscribed`, `last_read` on root messages and parents
