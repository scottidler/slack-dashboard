# Design Document: Heat Autoresearch, Findings and the Shipped Formula

**Author:** Scott Idler
**Date:** 2026-10-18
**Status:** Implemented

## North Star

Same sentence as the 2026-06-30 arena: the board exists to bubble the threads that need
Scott's eyes to the top, right now. This round replaced the synthetic arena's hand-written
criteria with four weeks of his real board and a label derived from what he actually did.

## Summary

Karpathy's autoresearch loop (github.com/karpathy/autoresearch) was ported onto the heat
formula: a fixed evaluator replays every work-hour look over a four-week corpus, one file
holds the formula, an agent edits only that file, keeps on improvement and reverts
otherwise. Against the involvement label (threads Scott stepped into) the loop lifted the
objective from 0.51 to 0.61 on the tuning weeks and 0.49 to 0.61 on a held-out week. The
keeps are four sentences about Scott, all driven by jev topic probabilities: an answered
thread leaves the board; a thread that needs a leader counts as full-size, escapes the
gate, and keeps its heat longer; heat decays hyperbolically over half a working hour.

What shipped: the tuned values in Scott's yml (code defaults stay neutral so the frozen
arena holds), jev rating inside the app so the live board scores on the same numbers the
loop tuned on, and an append-only event log of opens and dismisses, because the loop's
remaining gap is a missing signal, not a knob.

## The harness

| upstream     | here              | role |
|--------------|-------------------|------|
| `prepare.py` | `bin/replay.py`   | the corpus, the labels, the looks, the metric; never edited in a loop |
| `train.py`   | `bin/formula.py`  | the one file the loop edits |
| `program.md` | `bin/program.md`  | the rules, written by Scott |

Inputs: `bin/corpus.py` (548 threads, 29 channels, four weeks ending 2026-10-17),
`bin/topics.py` (jev-1.13.0 probabilities per thread: work, social, leader, incident,
access, resolved, question), `bin/judge.py` (jev's p(needs Scott) with the reader described
as Director over SRE, Data Platform and Security; 124 of 548 at p >= 0.6).

Looks: every work hour, 6 to 17 PT, on work days (240 looks; 180 train, 60 holdout). At a
look only the replies posted before it exist. Board membership (min replies, max age) comes
from the config and the harness overwrites whatever the formula says.

Keep rule: +0.005 on the train objective, equal with fewer lines also keeps. Holdout is the
last 7 days, printed every run, never steered on. Keep/revert is a file copy
(`formula-best.py`) rather than git because the worktree carries another session's work.

## The label

Three were built; the page and the harness compute every number from exactly one of them
at a time (the explorer's probability fit was once trained on one label while the objective
read another; the fix was structural: one `LABEL` predicate feeds everything).

- **habit**: Scott replied before the next look. Rejected by Scott: "I haven't been using
  the board to find items to respond to", so it measures where he already was, not what the
  board should have surfaced.
- **judge**: jev says the thread needed him. Useful as a cross-check, but the judge and the
  features both come from jev.
- **involvement** (the one used): a thread he stepped into (replied to, did not start) is
  needed at every look before his first reply; for purity, "the right kind of thread" is
  any thread he ever joined. 17 joined threads, 145 needed rows over the 180 train looks.

Metric: catch (per needed thread, best credit over its needed looks, 1 inside the top 10
else 10/rank) and purity (per look, share of the top 10 the label marks). Objective is the
mean. One needed row is worth about 0.7 points of objective.

## What the loop found

Live vs tuned on the involvement label, hourly looks, channel prior off, the loop's size
knobs (reply 1, participant 5, activity cap 50):

| formula | train | holdout | catch | purity |
|---------|-------|---------|-------|--------|
| live    | 0.513 | 0.494   | 0.873 | 0.153  |
| tuned   | 0.613 | 0.608   | 1.000 | 0.226  |

The keeps, in order, each a sentence about Scott:

1. `resolved_drop` 1.0: a thread jev reads as answered leaves the board.
2. `leader_weight` 45: a thread that needs a leader counts as a full-size thread.
3. Leader gate `0.3 + 0.7 * p(leader)`: a thread nobody needs a leader for is cut to 30%.
4. Hyperbolic atrophy `1 / (1 + t / half)` with half 0.5 work hours, stretched by
   `(0.5 + p(leader))`: heat is a morning thing, and a leader thread keeps it longer.
5. Channel prior off on train (the yml's weights cost 2 points there).

Ablated out as flat: the work gate, a leader x question vote, every size knob (reply
weight, participant weight, base cap and k, people cap), every burst knob (velocity,
window, activity cap), alive boost, social drop.

The sweep on the final formula is a plateau: no single knob moves the objective more than
2 points, and the leader vote at 0 loses only 1 point (it is nearly redundant with the
leader gate; both stayed because each cleared the threshold when added).

### The channel prior

Scott asked for the defaults to favor his direct reports, then his teams' internal
channels, then rooms shared with his peers and manager. Membership came from Slack and the
reporting lines from persona. Measured with the tuned formula, loop size knobs:

| prior                      | train  | holdout | catch | purity |
|----------------------------|--------|---------|-------|--------|
| live yml                   | 0.5911 | 0.5567  | 0.980 | 0.202  |
| org-derived                | 0.5921 | 0.6256  | 0.966 | 0.218  |
| off                        | 0.6128 | 0.6082  | 1.000 | 0.226  |
| live formula, live yml     | 0.5132 | 0.4941  | 0.873 | 0.153  |

The org prior beats the live yml on holdout by 7 points and ties it on train; it does not
beat no prior on train. It shipped because the order is what Scott asked for and it is the
better of the two priors that exist.

### The shipped shape

The yml's own size knobs (reply 2, participant 3, activity cap 20) differ from the ones the
loop carried as "live" (1, 5, 50). The sweep says they are flat; measured directly with the
org prior on:

| size knobs      | train  | holdout | catch | purity |
|-----------------|--------|---------|-------|--------|
| loop (1, 5, 50) | 0.5921 | 0.6256  | 0.966 | 0.218  |
| yml (2, 3, 20)  | 0.5878 | 0.5969  | 0.956 | 0.219  |

The yml keeps its size knobs; the shipped configuration is the second row: 7.5 points over
live on train, 10 on holdout.

## Holes found and closed

- **Board membership as a knob.** `max_thread_age_days` 1 "won" 6 points by moving needed
  threads to "never on a board" instead of catching them. Closed: membership comes from the
  config in `bin/replay.py`; the explorer's sweep no longer offers it.
- **The label's blind spot.** `involved_drop` toward 0 and "drop damping entirely" kept on
  the involvement label because the label is blind to a thread Scott read and left alone.
  Reverted by the taste rule in `bin/program.md`, rows logged.
- **Wrong baseline.** Run 0 carried a max age of 7 against a yml of 3, and later the size
  knobs above. The log was restarted from the true baseline; the size mismatch was
  measured rather than re-run because the sweep showed it flat.
- **Two labels on one page.** The calibrated-probability fit was trained on the habit label
  while the objective read the involvement label. Closed by the one-label refactor.

## The remaining gap is a signal, not a knob

At any look, three of the four threads in the top 10 are ones Scott never joined. Catch is
done (96 to 100%). Nothing in the inputs (size, silence, jev topics) separates "he joined"
from "he did not", and the involvement label cannot see a thread he opened and read. The
dashboard already has the two actions that would: the dismiss control and the click-through
to Slack. Logging them is the whole of item 2 below.

## Decisions

1. **Ship the tuned values through the yml, not `HeatConfig` defaults.** The code defaults
   for every new knob are neutral (`atrophy-curve: exponential`, stretch 0, leader weight 0,
   gate floor 1.0, resolved drop 0), so `HeatConfig()` is the pre-topic formula and the
   frozen arena in `tests/calibration/` keeps passing untouched. Scott's yml carries the
   tuned set and the org-derived channel weights. This is the same pattern as
   `velocity-weight` (0 in code, 5.0 in the yml).
2. **Rate threads with jev inside the app.** `src/slack_dashboard/topics.py` owns the
   questions, the state and the fail-closed parse; the app's `TopicRater` and the `bin/`
   tools both use it, so the board scores on exactly the numbers the loop tuned on. One
   call per thread on first sight and again when it grows (a watermark, like the summary).
   A failed call leaves the thread unrated and neutral. `topics.api-key` empty means no
   rating; a topic knob on with no key fails at boot.
3. **An unrated thread is neutral, never penalized.** The loop scored unrated threads with
   p = 0 (every corpus thread was rated). In the app a thread is unrated for the seconds
   between fetch and rating, and a 0.3 gate on every new thread would hide exactly the
   fresh ones. Votes 0, gates 1, plain half-life until rated.
4. **Log opens and dismisses.** `events.jsonl` beside the config, one JSON line per action
   with the thread's rank, score and tier at that moment (`src/slack_dashboard/events.py`).
   Opens ride a `sendBeacon` on the row link so the `slack://` navigation is never held up.
   Nothing in the app reads the file; the next loop joins it to the corpus.
5. **Two chips on the strip.** 🎯 p(needs a leader) and ✅ p(resolved), `?` until rated,
   dimmed when no knob reads them.

## Config keys (all under `heat:` unless noted)

| key | default | tuned |
|-----|---------|-------|
| `atrophy-curve` | `exponential` | `hyperbolic` |
| `atrophy-half-life-work-hours` | `3.0` | `0.5` |
| `atrophy-leader-stretch` | `0.0` | `1.0` |
| `leader-weight` | `0.0` | `45` |
| `leader-gate-floor` | `1.0` | `0.3` |
| `resolved-drop` | `0.0` | `1.0` |
| `topics.api-key` | `""` | `${TYPESAFE_API_KEY}` |
| `topics.model` | `jev-1.13.0` | same |

Channel weights shipped (the org tiers): security-internal and platform-leadership 3.0;
sre-internal, data-platform-internal, platform-internal 2.6; engineering-leadership 2.4;
tech-staff-team and engineering-mgmt 2.2; it-security and incidents 2.0; opex-monthly 1.6;
sre-sec 1.5; ask-security 1.4; sre, data-platform, eng-on-call 1.3; sre-it 1.2;
sre-solutions, it-helpdesk, tech-spec-reviews 1.0; engineering 0.8; ai-foundry,
ai-technical 0.7; enterprise-ai-onboarding-and-configuration, backstage, proj-mintlify 0.6;
staging-env, cloud-costs 0.5.

## Events format

```
{"kind": "open", "channel_id": "C...", "thread_ts": "1760....", "ts": 1760..., "at": "2026-10-18T...",
 "on_board": true, "channel": "sre-internal", "rank": 4, "score": 31.2, "tier": "warm",
 "message_count": 7, "topics": {"leader": 0.81, "resolved": 0.07, ...}}
```

`kind` is `open` or `dismiss`. `on_board: false` carries no position (the thread was not
in the ranking at that moment). The dismiss is recorded before eviction so it keeps its
position.

## Next

1. Let `events.jsonl` accumulate a few weeks.
2. Add an `events` join to `bin/corpus.py` output and an "acted" label to `bin/replay.py`
   (opened or dismissed), alongside the three existing labels.
3. Rerun the loop on that label. Expect the purity ceiling to move; expect the involvement
   keeps to hold.

## Verification

- `otto ci` green: 404 tests, including the frozen arena on the unchanged defaults, the
  seven new heat predicates (neutral when unrated, defaults are the old score, resolved
  gate, leader vote and gate, hyperbolic half point and tail, leader stretch, single path
  with topics), the config bounds and the boot check, the jev module (state, parse, retry,
  fail closed), the event store, the open route, the chips, and the poller wiring.
- The replay numbers above are from `bin/replay.py` on the 2026-10-17 corpus and are
  reproduced by the commands in `bin/program.md`.
