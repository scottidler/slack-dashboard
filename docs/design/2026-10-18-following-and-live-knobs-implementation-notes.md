# Implementation Notes: Following, and the Knobs on the Live Board

Design doc: `docs/design/2026-10-18-following-and-live-knobs.md`

## Phase 1: Following

### Design decisions
- Root found by `ts == thread_ts`, follow state copied by one helper | `slack/poller.py:_apply_follow_state` | the full fetch, the incremental fetch and the metadata sweep all go through it, so "a root without `subscribed` leaves both fields and warns" lives in one place.
- The full-fetch rebuild carries `subscribed` / `last_read` forward from the existing entry, then applies the root | `slack/poller.py:_fetch_thread` | a rebuild whose root lacks the key keeps the known state instead of resetting to unfollowed.
- `last_read` is `None` when the root has no `last_read` (Slack omits it on unfollowed threads) | `_apply_follow_state` | unknown read state, counted as no unread replies by `following_damping`.
- The sweep skips parents with no replies | `slack/poller.py:_sweep_metadata` | Phase 0 showed Slack puts `subscribed` only on parents with replies; without the skip a `min-replies: 0` channel would warn on every reply-less parent every interval.
- The sweep catches per channel and logs | `_sweep_metadata` | it runs inside `_refresh_loop`, which has no other exception guard; one failed history call must not kill the refresh loop for the life of the process.
- The sweep is awaited inline after `seed_channels` in `_refresh_loop` | it shares the client's history semaphore with the queued channel fetches, so the pacing stays the client's.
- Listener reads my id through a zero-arg callable | `slack/listener.py:SocketListener.__init__`, wired `self_user_id=lambda: poller.self_user_id` in `main.py` | the poller resolves it in `start()` after the listener is built; a test proves the value is read at event time.
- `self_user_id` removed from `web._flags`, `web._build_row` and `web.group_threads` as well as the heat signatures | its only use there was `is_involved` and the heat call; leaving it would be a dead parameter.
- Non-negative validation is one loop over every numeric `HeatConfig` field plus both weight maps, with an explicit signed allowlist (`atrophy_leader_stretch`) | `config.py:HeatConfig._validate_non_negative` | a new knob is covered by default instead of needing a line added.
- Retired keys raise naming the replacement | `config.py:_RETIRED_HEAT_KEYS`, `_reject_retired_keys` | covers the two just-renamed keys and the three older ones the old validator warned about, in kebab and snake spelling.
- `display.marks.me` gets its own error naming `following` | `config.py:DisplayConfig._validate_marks` | the generic "unknown keys" error would not say what to type.
- Arena `_rebuild_variant` is `dataclasses.replace(thread, subscribed=True, last_read=...)` | `tests/calibration/criteria.py` | replies, participants, message count and timing are untouched by construction, so the rebuild half tests the rebuild only.
- Added a second arena bite test beside the required one, `test_involvement_criterion_fails_with_no_drop` (`followed_drop = 1.0`) | `tests/calibration/test_calibration.py` | proves the drop half bites too.
- Poller follow-state tests grouped in `class TestSubscribed` | `tests/slack/test_poller.py` | the success criterion selects with `-k subscribed`, and `test_unfollow_without_new_reply_reaches_the_board` has no "subscribed" in its own name; the class name carries the keyword.
- `bin/formula.py` `KNOBS` carries the live yml's following values (0.2 / 0.0), marked owner policy; `bin/replay.py:run` overwrites both from the yml exactly as it does membership | verified: a scratch formula with 0.9 / 0.5 replays to the same 0.5167 / 0.4814.

### Deviations
- `test_decay_hours_wins_over_legacy` inverted to `test_decay_hours_with_legacy_key_is_an_error`: with `extra="forbid"` a leftover `decay-half-life-hours` beside `decay-hours` now fails at boot instead of being silently ignored. The legacy-only case still migrates (`_migrate_decay_half_life` now pops the legacy key, which `extra="forbid"` requires).
- `test_deprecated_involved_key_warns` / `test_all_deprecated_involved_keys_warn` replaced by `test_retired_involved_keys_are_errors_naming_the_replacement` (parametrized over all five keys): the doc turns the warnings into errors.
- The involvement tests in `tests/test_heat.py` and `tests/test_web.py` (`is_involved`, `involvement_damping`, `_flags(..., self_user_id)`) were rewritten against `is_following` / `following_damping` / `subscribed`, not deleted: each old case has a following counterpart (off when not following, disabled at drop 1.0, full drop, rebuild, clamp, compute_heat applies the drop, mark leads the row, mark renders end to end).
- README.md and CLAUDE.md lines naming `involved-*` / 🙋 updated to the new names. Not listed in the Phase 1 bullets, but they would otherwise name keys the app now refuses.
- The heat-strip damping chip keeps its 👤 glyph; its tooltip and constant now say "following damping" (`web._FOLLOWING_DAMPING`). The doc does not mention the chip.
- `bin/explore.html`: knob keys and help text renamed only. Its in-page "kind" purity still credits ever-joined threads (the explorer's own JavaScript objective, not the harness); not changed in Phase 1.

### Tradeoffs
- Sweep as a separate `fetch_threads(oldest=None)` per channel vs. reading the follow state off the incremental channel fetch: the incremental fetch only returns new parents, so it cannot carry an unfollow on a quiet thread. The doc's one-call-per-channel cost was accepted.
- Harness `following` = "started or posted before the look" (same arithmetic as the old `i_posted`) vs. trying to model mentions: the corpus has no subscription history; recorded as the known gap on `Features.following`.

### Open questions
- Replay on the committed harness with the live yml knobs (drop 0.2, rebuild 0), default schedule: `objective: 0.5167`, `holdout: 0.4814` (`all: 0.5099`; train catch 0.986 purity 0.047; holdout catch 0.944 purity 0.018). Matches the scratch measurement exactly. The Background table's post-change row is for the parent to record.
- Pre-existing, not touched: `_fetch_thread` sets the thread watermark to `max(ts)` of the response, so an incremental fetch whose `oldest` is past every reply returns the root alone and moves the watermark back to `thread_ts` (Phase 0 confirmed that response shape). Worth a look before leaning on the incremental path more.
- The live yml (`~/repos/scottidler/dotfiles/HOME/.config/slack-dashboard/slack-dashboard.yml`) was edited but not committed; the dotfiles worktree already carried other uncommitted edits to the same file before this phase.
- The deployed image will refuse the new yml's keys and the new image refuses the old ones; per the doc's rollout, preflight the new yml against the new image before restarting.

## Phase 2: Pure scoring and request overrides

### Design decisions
- `Ranked(entry, score, tier)` is a frozen dataclass; `score_threads` scores with `heat_breakdown(t, cfg, now).overall` at one `now` for the whole board | `heat.py:score_threads` | one instant per board makes two renders comparable byte for byte. `write_back` is the one mutating helper; `rank_threads` and `SlackPoller.ranked_threads` are `write_back(score_threads(...))`.
- `SlackPoller.eligible(config, now)` takes the heat config and `now` | `slack/poller.py:eligible` | a narrowed `max-thread-age-days` override must narrow membership, so the stale filter runs under the request's config; `filter_stale_threads` gained an optional `now` for the same reason.
- Overrides are limited to `KNOBS` keys plus `heat.channel-weights.<name>` / `heat.people-weights.<name>` | `knobs.py:parse_overrides` | a scalar `HeatConfig` field outside the panel (`decay-hours`, the mark knobs) is a 422 "unknown override", so the override surface is exactly the panel's and the mark knobs (a Non-Goal) cannot move. Snake spelling (`heat.velocity_weight`) is also unknown: one spelling per key.
- 422 body is plain text, one line per pydantic error, located by the query key (`heat.velocity-weight: Input should be a valid number, ...`) | `knobs.py:_describe`, `web.py:_override_rejected` | Phase 3 puts the body into `#knob-error.textContent`; pydantic's multi-line dump with a docs URL is noise there.
- The dependency raises `OverrideError(ValueError)`, and `create_routes` registers one exception handler for it | `knobs.py:request_config`, `web.py:create_routes` | a FastAPI dependency cannot return a response; a dedicated type keeps unrelated `ValueError`s out of the 422 path.
- `request_config(base)` is a factory returning the dependency, used via `Annotated[RequestConfig, Depends(...)]` | `knobs.py` | the base config is the `create_routes` argument, not app state; `Annotated` keeps ruff's B008 clean.
- `apply_overrides` with no overrides returns the yml config object itself | `knobs.py:apply_overrides` | the no-override path is the yml path, not a re-validated copy.
- `data-overrides` is rendered only when the request had overrides | `templates/partials/{threads,summary,channel}.html` | keeps the no-override render byte-identical (the doc asks for both).
- The open beacon and the dismiss request carry the surface's overrides as `heat.*` query parameters, read from the clicked partial's root (`.channel-pop-inner`, `.summary-content`, `.disclosure`, nearest first) | `templates/index.html:withSurfaceOverrides`, `htmx:configRequest` listener | the event routes then go through the same `request_config` dependency, so a recorded map is validated by the same code that rendered it. Nearest-root lookup stops a popover rendered without overrides from inheriting the board's.
- `_record_event` writes `overrides` only when the map is non-empty; the rank stays the yml's | `slack/poller.py:_record_event` | per the doc's events.jsonl note.
- `_flags` no longer logs `thread.heat_tier` | `web.py:_flags` | under overrides the stored tier is not the row's tier; logging it would mislead.
- `test_knobs.py` freezes time by replacing `datetime` in `heat`, `slack.poller` and `web` | no new dev dependency (no freezegun in the repo).
- The byte-identical test pins sha256 of four renders (`/threads`, `/threads?group-by=channel&compact=false`, `/summarize/<followed>`, `/channel/C1`) taken from commit 4a5fb49's code on the same fixture board and frozen clock; the new code reproduces all four. A deliberate template change re-baselines them.
- Added `CLAUDE.md` Architecture line for `knobs.py` (module list would otherwise be stale).

### Deviations
- `group_threads` / `_build_row` take `list[Ranked]` / `Ranked` (the doc says "take `Ranked` and the request's config"; the config was already a parameter). Same effect.
- `KNOBS` ranges: `activity-cap` steps by 1 (lo 1, hi 100) rather than the explorer's 5, so `hi` is reachable from `lo`; choice knobs (`atrophy-curve`, `tier-method`) carry `lo = hi = step = 0` and their `choices`, and the range round-trip test validates every choice instead.
- The `KNOBS` coverage and range round-trip tests (described under API Design, not in the Phase 2 bullets) landed here, since `KNOBS` did.
- `tests/test_web.py`: mock pollers now stub `eligible` instead of `ranked_threads` (the routes no longer call the mutating wrapper); `test_threads_row_renders_strip_age_and_twelve_mark_slots` now asserts `heat-cold` instead of `heat-hot`, because the row tier comes from scoring the fixture (last post months ago) and no longer from the entry's stored `heat_tier="hot"`. The two event route tests assert the new `overrides` argument (`None` without overrides).
- `tests/test_grouping.py`: fixtures wrap entries in `Ranked` carrying the fixture tier; added `test_build_row_tier_comes_from_the_ranking_not_the_entry`.

### Tradeoffs
- Overrides on the event routes as query parameters vs a JSON body: the beacon cannot set headers, and `request.form()` needs `python-multipart` (not a dependency). The query form reuses `parse_overrides` and needs no new package.
- Golden hashes vs comparing against a re-rendered baseline in the test: the literal "byte-identical to today's render" needs a fixed reference; the hashes are opaque, so the test docstring says how they were produced.
- Restricting overrides to the panel's knobs vs accepting any scalar `HeatConfig` field (the doc's literal wording): fail-closed, and the coverage test keeps the two lists honest.

### Open questions
- Hover requests (`/summarize`, `/channel`) carry no overrides until Phase 3's hidden form (`hx-include`), so on the Phase 2 build only a hand-typed URL exercises the override path on those two routes.
- The local `slack-dashboard` container on desk is bound to 8889; the success criteria ran on a local `uv run` instance on 8899 instead (see the hand-back for output).
- Success criterion 2 on live data (local instance, 2026-10-18 Sunday afternoon): `md5sum` of `/summarize/C039YLDJW5T/1792195826.029839` with and without `heat.velocity-weight=60` differs (`e3674205...` vs `6234a06f...`), but the only byte that differs is the root's `data-overrides`: none of the 58 board threads had a reply inside the 30-minute velocity window (or a 240-minute one), so the strip itself was the same. The strip-level proof is `test_velocity_override_changes_the_strip_on_a_thread_with_replies_in_the_window` (fixture thread with five replies in the window: 🌡29 -> 🌡41). Worth re-running the curl form on a weekday.

## Phase 3: The panel

### Design decisions
- `panel_groups(config)` builds the panel's data (`PanelGroup` / `PanelKnob`, the yml value pre-formatted, the tick pre-computed as a percent) in `knobs.py`; `GET /knobs` only renders it | `knobs.py:panel_groups`, `web.py:knobs_panel` | the formatting and the dimmed rule are testable without a template, and the panel stays generated from `KNOBS`, the one list.
- `/knobs` renders from the yml and ignores request overrides | `web.py:knobs_panel` | the panel shows what the yml says; the viewer's values layer on in the browser (hydration), so a pasted link cannot change what the tick means. Tested.
- Each row is a `div.knob` carrying `data-knob="<query key>"` and `data-yml`, on its own line | `partials/knobs.html` | the Phase 3 criterion is `rg -c` (lines, not matches); one attribute per line makes the count equal the row count. The inputs inside the row carry no `data-knob`.
- The overrides map holds only knobs that differ from the yml (a value set back to the yml's deletes the key) | `index.html` `set()` | "Only changed knobs are in the URL" (Risks table); numeric knobs compare as numbers, choice knobs as strings.
- The yml value is a tick (`i.knob-tick`, `left: N%`) under the range input; a yml value past a knob's range clamps to 0 or 100, and a weight-map slider's `hi` widens to the yml value when it is above 3 | `knobs.py:_tick`, `_map_group` | the explorer's 0..3 would otherwise make a yml weight above 3 unrepresentable on its own slider.
- Weight-map sliders run 0..3 step 0.1, the explorer's channel range; people weights reuse it | `knobs.py:MAP_WEIGHT_*` | the explorer has no people-weight sliders; same range is the nearest honest copy.
- Debounce 150 ms on every change, one `poll` trigger; `reset`/`reset all` go through the same `changed()` | `index.html` | per the doc; an in-flight request is not cancelled.
- `htmx:responseError` / `htmx:afterRequest` listeners on `#thread-list` filter on `pathInfo.requestPath` starting with `/threads` | `index.html` | hover, popover and dismiss requests originate inside the list and must not touch the board's error box. `#knob-error` sits above the board, outside the panel, so it shows with the panel closed.
- `copy as yml` writes into a hidden `<pre id="knob-yml">` (shown on click) and the clipboard when available; map entry names that are not plain words are JSON-quoted (valid YAML) | `index.html:renderYml` | a glob channel name like `*-sre` would otherwise be a YAML alias.
- `hx-include="#knob-form"` is on `#thread-list` beside the `hx-vals` spread, as the doc says | `index.html` | measured below: the hover request carried the map once.

### Deviations
- None from the Phase 3 bullets. `panel_groups` is an addition (the data seam the doc did not name).

### Tradeoffs
- Panel data built in Python vs formatting in Jinja: Python, so the tick math and the dimmed rule are plain-function tests.
- Hand-written JS in `index.html` vs a static file: the page already holds its JS inline; Phase 4 moves only the chart code into a static file, as the doc says.

### Open questions
- Shakedown (success criterion 2) ran on a LOCAL instance, not the deployed board (the deployed board is not updated until finalization). Setup: scratch copy of the live yml with only `server.host/port` changed (127.0.0.1:8899), empty app token, headless `/usr/bin/google-chrome` driven by playwright (`uv run --with playwright`), 2026-10-18 Sunday, live Slack data (73 `data-knob` rows). Exercised and observed:
  1. Load: panel hidden; `knobs` toggle opens it; 73 `.knob` rows.
  2. Range input `heat.velocity-weight` set to 60 with an `input` event: URL became `/?heat.velocity-weight=60`; board order changed (a different thread entered the top 8 at rank 7); the row got the `changed` class.
  3. Also set `followed-drop` to 1 via the number input: URL `/?heat.velocity-weight=60&heat.followed-drop=1`; top 8 reordered again.
  4. Hover on a `#channel` handle fired `/channel/C023YTN4B1D?group-by=none&compact=true&heat.velocity-weight=60&heat.followed-drop=1`: the overrides reach hover requests, once each.
  5. Reload of that URL: top 8 identical to the pre-reload board and to a direct `curl /threads?heat.velocity-weight=60&heat.followed-drop=1`; inputs hydrated (`followed-drop` number 1, velocity range 60).
  6. `copy as yml` produced `heat:\n  velocity-weight: 60\n  followed-drop: 1`; merged over the scratch yml it loads with `load_config` (velocity 60.0, drop 1.0). A second run with a select, a channel weight and a people weight (`atrophy-curve: hyperbolic`, nested `channel-weights.sre-internal: 2.5`, `people-weights.U0AAA: 4`) also loads, values round-tripped; the select hydrated from the URL.
  7. A value the server rejects typed after a good render (`velocity-weight` -5): `#knob-error` read `Value error, heat velocity-weight (-5.0) must be >= 0`, and the board's top 8 was unchanged. `reset all` cleared the URL and the error, and the board matched a direct `curl /threads`.
  8. Bad value on first load (`/?heat.velocity-weight=abc`): `#knob-error` read `heat.velocity-weight: Input should be a valid number, unable to parse string as a number`; the board stayed `Loading threads...`.
  NOT VERIFIED: any of this on the deployed board (not yet deployed); a real drag with the mouse (the range input was set by script plus an `input` event); the system clipboard contents (the `<pre>` text was read instead).
- A note on the 422 text for a negative: pydantic's own wording (`Value error, heat velocity-weight (-5.0) must be >= 0`) comes from the Phase 1 validator; unchanged here.
- A typed non-number cannot reach the server from the panel (`type=number` refuses it), so the first-load URL is the only path that shows the `could not parse` form.
- Shakedown step 2 of Phase 2's notes said no board thread had replies in the velocity window on a Sunday; today's run did reorder at `velocity-weight=60`, so the live-data proof the Phase 2 open question asked for now exists for `/threads`.

## Phase 4: Shaping charts on live data

### Design decisions
- `chart()` and its helpers (`hist`, `FLAT`, `esc`, `fmt`) moved verbatim into `src/slack_dashboard/static/chart.js`; `bin/explore.html` loads it with `<script src="../src/slack_dashboard/static/chart.js">` and the app page with `<script src="/static/chart.js">` | `static/chart.js`, `templates/index.html`, `bin/explore.html` | the chart code exists once. `esc` and `fmt` moved too because `chart()` needs them and the explorer's copies were identical; the explorer no longer defines them.
- The published explorer is one standalone file built by `bin/explore.py`, so `render_page(payload)` replaces that exact script tag with the file's contents inline, then substitutes the data; it raises `ValueError` if the template no longer carries the tag | `bin/explore.py:render_page` | fail loudly: a template that stops loading the shared file cannot silently publish a page without `chart()`. `main` calls it. Checked: the rendered page's two inline scripts both pass `node --check`.
- `GET /knobs/inputs` returns a JSON list, one row per thread `/threads` would render under the same overrides, each `{volume, since-last, p-leader, score}` (kebab keys per the repo's config-key convention; `p-leader` is null until jev rates the thread) | `knobs.py:shaping_inputs`, `web.py:knobs_inputs` | the Phase 4 criterion needs a bare list, so the curve parameters are not in the response; the page already knows them (override map, else the panel row's `data-yml`). Same `request_config` dependency, `_rank`, and one `now` as `/threads`, so rows and scores match the board.
- `volume` is `message_count * reply_weight + people_term` under the request's config | `knobs.py:shaping_inputs` | `HeatBreakdown` carries the parts but not the sum; computed here with the same expression as `heat_breakdown` rather than adding a field to the shared struct.
- `chart()` takes `fn === null` to draw the bars alone | `static/chart.js` | the live app has no logistic fit, so the score distribution has no curve; the explorer's own calls are unchanged.
- Four charts under the panel: volume -> base, hours since last -> atrophy, p(leader) -> vote (omitted when no thread is rated), score distribution. The explorer's other three (replies in window -> activity, alive boost, p(social) -> gate) are not ported; the design names four | `templates/index.html:drawCharts`.
- Charts redraw on every successful `/threads` response (`htmx:afterRequest`), on opening the panel, and after the panel hydrates; a request counter drops a stale response. Nothing is fetched while the panel is closed | `templates/index.html`.
- `app.mount("/static", StaticFiles(...))` in `create_routes` | `web.py` | the app had no static route; Starlette's `StaticFiles` needs no new dependency, and the Dockerfile already copies `src/`.
- Chart styles are scoped to `#knob-charts` with the pico variables for muted text and the explorer's bar and curve colours | `templates/base.html`. Dense: no figure border, no padding.
- CLAUDE.md Architecture gains a `static/chart.js` line.

### Deviations
- None from the Phase 4 bullets. The doc's "four charts" lists volume, hours since last, p(leader), score distribution; the explorer's replies-in-window, alive and p(social) charts stay explorer-only.

### Tradeoffs
- Inlining at build time vs a published `<script src>` to the app: marquee posts are standalone behind Okta and cannot reach the app's `/static`; inlining keeps the published file one self-contained document.
- Returning rows only vs rows plus curve parameters: the criterion fixes the response as a list; the knob values are already in the browser.

### Open questions
- The published explorer on marquee still carries the old inline `chart()`; the republish (`marquee replace`) is the finalization step and was not run.
- Success criteria ran on a local instance on port 8899 (the scratch yml's port; 8889 is the docker container). Criterion 1 on live data: `/knobs/inputs` row count 58 and `/threads?compact=false` row count 58 (`rg -c 'class="thread-row'`), after the startup backfill had settled. Criterion 2: `rg -c "function chart\(" bin/explore.html src/slack_dashboard/static/` printed only `src/slack_dashboard/static/chart.js:1`.
- Browser check (headless `/usr/bin/google-chrome` via playwright): opening the panel drew 4 `figure`s (30 bars each; curve path on the first three, none on the score chart). Setting `base-k` to 40 and `velocity-weight` to 60 changed the volume chart's note from `v / (v + 15)` to `v / (v + 40)`, redrew the score distribution, and the URL carried both; no page errors. NOT VERIFIED: a real mouse drag, and the deployed board.
