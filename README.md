# slack-dashboard

A single, scannable triage pane over the Slack channels you can no longer skim.
It monitors channels in real time (Socket Mode + REST hybrid), heat-ranks threads
you are **not** tagged in, and surfaces them as dense, one-line rows so you can
catch what skimming would have caught - without hiding anything.

The headline guarantee: **stay clean through density, not filtering.** No
trackable live thread is ever hidden by the tool's relevance judgment.

## How ranking works

Each thread gets an explainable, hand-tunable heat score built on a single
arithmetic path:

```
base-norm   = base-cap * volume / (volume + base-k)          # hard asymptotic ceiling
activity    = min(activity-cap, velocity * velocity-weight)  # bounded burst, outside the ceiling
votes       = leader-weight * p(leader)                      # jev: a leader thread is full-size
half        = atrophy-half-life-work-hours * (1 + atrophy-leader-stretch * (p(leader) - 0.5))
atrophy     = 0.5 ** (work_hours_since_last / half)          # or 1 / (1 + t / half)
alive-boost = 1 + alive-weight * f(time_alive) * atrophy     # freshness-gated longevity lift
gates       = (floor + (1 - floor) * p(leader)) * (1 - resolved-drop * p(resolved))
score       = channel-weight * (base-norm + activity + votes) * atrophy * alive-boost * damping * gates
```

- **channel-weight** - the highest-leverage knob. Weight the channels you care
  about (`sre`, `data-platform`) up and the noise (`proj-*`) down. Supports glob
  patterns; exact keys win over globs.
- **base-norm** - a hard, monotone asymptotic ceiling on volume
  (`base-cap`/`base-k`): a huge stale thread and a modest fresh one both approach
  `base-cap`, so raw volume can no longer dominate the board.
- **activity** - a bounded additive burst term (`activity-cap`) kept *outside*
  the volume ceiling, so a short thread spiking *now* is not washed out by a big
  thread's message-count saturation. Defaults to off (`velocity-weight: 0`).
- **atrophy** - an exponential half-life decay measured in *working hours*
  (`atrophy-half-life-work-hours`), evaluated over the 6am-6pm PT Mon-Fri work
  window (`heat.work-window`). Nights and weekends contribute zero working hours,
  so a Friday-afternoon thread does not go stone-cold over the weekend.
- **alive-boost** - a freshness-gated longevity lift (`alive-weight`/`alive-k`):
  it lifts a long-lived thread only while it is still fresh, since the `* atrophy`
  gate collapses the boost back toward 1.0 once the thread goes idle. Ships
  display-only (`alive-weight: 0`).
- **following damping** - a thread Slack follows for you (started, commented, @'d,
  or followed by hand; Slack's `subscribed` on the root) drops to `followed-drop`,
  then each reply after your Slack `last_read` rebuilds it back toward 1.0 at rate
  `followed-rebuild-per-msg`.
- **topic terms** - jev (TypeSafe's typed-decision model) rates each thread for
  p(needs a leader) and p(resolved). A leader thread gets a vote (`leader-weight`),
  escapes the leader gate (`leader-gate-floor`) and keeps its heat longer
  (`atrophy-leader-stretch`); an answered thread leaves the board (`resolved-drop`).
  All ship neutral; an unrated thread always scores as if they were off. The
  autoresearch loop (`docs/design/2026-10-18-heat-autoresearch.md`) set the tuned
  values in the example yml.
- **tiering** - rank-aware relative tiering by default (`tier-method: relative`):
  the top `tier-hot-count` threads paint hot and the next `tier-warm-count` warm,
  with an absolute `tier-floor` that stops a fully-atrophied board from painting
  its top-N hot. Set `tier-method: absolute` to tier on raw score thresholds
  (`tier-hot`/`tier-warm`) instead.
- **resurrection** - a long-dead thread that gets fresh activity floats back to
  the top with a `zombie` marker.

Two removal concepts, kept distinct:

- **dead** - automatic, reversible: a thread past `max-thread-age-days` with no
  activity drops off, but resurrects if it gets new replies.
- **dismissed** - manual, permanent: one click removes a thread for good. The
  dismiss list is the only state that survives a restart (`dismissed.jsonl`).

## Configuration

Config lives at `~/.config/slack-dashboard/slack-dashboard.yml` (override the
directory with `XDG_CONFIG_HOME`). All keys are kebab-case. New ranking knobs
default to current behavior - the first run after upgrade ranks identically to
before (neutral weights, zero velocity). See
[`slack-dashboard.example.yml`](slack-dashboard.example.yml) for a full,
commented starting point.

### Key knobs

| Key | Default | Meaning |
|-----|---------|---------|
| `workspace` | `""` | Workspace subdomain for Slack deep links (e.g. `tatari`). |
| `heat.channel-weights` | `{}` | Per-channel multiplier (glob-aware). e.g. `sre: 2.0`, `proj-*: 0.5`. |
| `heat.velocity-weight` | `0.0` | How much a currently-spiking thread is boosted (feeds `activity`). |
| `heat.velocity-window-minutes` | `30` | Window over which velocity is measured. |
| `heat.atrophy-half-life-work-hours` | `3.0` | Exponential half-life of atrophy in *working* hours (6am-6pm PT Mon-Fri). |
| `heat.base-cap` | `50.0` | Asymptotic ceiling `base-norm` approaches as volume grows. |
| `heat.base-k` | `15.0` | Half-saturation point of the `base-norm` ceiling (in volume units). |
| `heat.activity-cap` | `20.0` | Upper bound on the additive `activity` burst term. |
| `heat.alive-weight` | `0.0` | Strength of the freshness-gated longevity lift (`0` = display-only). |
| `heat.alive-k` | `6.0` | Half-point of the time-alive ramp (working hours of thread life). |
| `heat.followed-drop` | `0.8` | Score multiplier on a thread Slack follows for you (`1.0` disables). |
| `heat.followed-rebuild-per-msg` | `0.15` | How fast replies after your Slack `last_read` rebuild toward 1.0. |
| `heat.atrophy-curve` | `exponential` | `exponential` (`0.5 ** (t/h)`) or `hyperbolic` (`1 / (1 + t/h)`, same half point, longer tail). |
| `heat.atrophy-leader-stretch` | `0.0` | Half-life multiplier `1 + stretch * (p(leader) - 0.5)`. Tuned: `1.0`. |
| `heat.leader-weight` | `0.0` | Additive vote `leader-weight * p(leader)`. Tuned: `45`. |
| `heat.leader-gate-floor` | `1.0` | Gate `floor + (1 - floor) * p(leader)`; `1.0` is off. Tuned: `0.3`. |
| `heat.resolved-drop` | `0.0` | Gate `1 - resolved-drop * p(resolved)`. Tuned: `1.0`. |
| `topics.api-key` | `""` | jev (TypeSafe) key. Empty = no topic rating; required when any topic knob is on. |
| `topics.model` | `jev-1.13.0` | Pinned jev model; an answer from any other model is rejected. |
| `heat.tier-method` | `relative` | `relative` (rank-aware top-N) or `absolute` (raw score thresholds). |
| `heat.tier-hot-count` | `3` | Relative mode: number of top threads painted hot. |
| `heat.tier-warm-count` | `10` | Relative mode: number of next threads painted warm. |
| `heat.tier-floor` | `5.0` | Relative mode: absolute floor below which a thread is never hot/warm. |
| `heat.tier-hot` / `heat.tier-warm` | `50.0` / `20.0` | Absolute mode: raw score thresholds for hot / warm. |
| `heat.decay-hours` | `24` | Legacy. No longer feeds the main score; still used by the 🔥 heated-exchange signal. |
| `heat.decay-floor` | `0.01` | Legacy. No longer read by the main score (retained for backward-compat). |
| `heat.resurrection-gap-hours` | `24` | Quiet gap after which new activity counts as a resurrection. |
| `heat.resurrection-age-days` | `2` | A thread must be older than this to qualify as a zombie. |
| `heat.resurrection-display-hours` | `24` | How long the `zombie` marker shows after revival. |
| `fetch.min-replies` | `3` | Global definition of "a thread" (vs. a lone post). |
| `fetch.channel-min-replies` | `{}` | Per-channel override (glob-aware). Set `1` for high-stakes ops channels. |

Tuning currently requires a config edit + restart (config is loaded once at
startup; there is no hot-reload yet).

## UI

- **Compact rows** - `Nm|Np  #channel  strip  Nh  marks  title`, grouped (default by
  channel). Every trackable thread renders - there is no row cap. The strip is one box
  per working hour over the last 12 (newest right; height = posts in that hour: 1, 2-3,
  4-7, 8+; a floor line for none), `Nh` is working hours since the last post, and the
  marks are twelve fixed slots, lit when the state applies and dim when not: 👔 needs
  a leader, 🔑 access, 🚨 incident, ✅ resolved, 🎉 social (jev, 60%+), then 🔔 Slack
  follows it for you, 👑 vip, ✨ new, 📈 spiking, 🌶️ heated, ⏪ revived, ❓ unanswered. Hover a slot
  for its meaning. The glyphs are `display.marks` in the yml.
- **Group by** - `channel | size | velocity | participants`.
- **Hover** a row for the AI summary; **click** to open the Slack thread.
- **Dismiss** - the small control on each row; one click, permanent.
- **Knobs** - the ⚙ in the left rail opens a sidebar of every heat knob
  (size, time, burst, following, topics, tiers, then the channel and people weights), each
  with the yml's value as a tick. Drag one and the server re-ranks the current data with it;
  `reset` per knob, `reset all`. Only changed knobs are in the URL
  (`?heat.velocity-weight=60`), so a reload or a pasted link reproduces the board. A value
  the validator rejects shows its message above the board, which keeps its last render.
  The shaping charts sit below the board while the sidebar is open.
  `copy as yml` renders the changed knobs as a `heat:` block to paste into the yml by hand:
  the site never writes the config. Opens and dismisses made from an overridden board are
  logged with their `overrides`.

Opens and dismisses are appended to `events.jsonl` beside the config, each with
the thread's rank, score and tier at that moment. They are the "what did I act
on" label the Slack history cannot give; nothing in the app reads them back.

## Tuning harness

The heat knobs are set by measurement, not by hand. `bin/` holds the loop
(dev tools, run with `uv run`, never wired into the app or CI):

| Tool | Role |
|------|------|
| `bin/corpus.py` | Pull weeks of thread history for the configured channels into one jsonl corpus. |
| `bin/topics.py` | Rate every corpus thread with jev (the app's own questions and state). |
| `bin/judge.py` | Ask jev whether each thread needed the reader (an alternative label). |
| `bin/replay.py` | The fixed evaluator: replay every look, score with `bin/formula.py`, print the objective. |
| `bin/formula.py` | The one file the autoresearch loop edits. |
| `bin/program.md` | The loop's rules. |
| `bin/explore.py` | Build the knob explorer page (`bin/explore.html`) over the corpus. |
| `bin/calibrate.py` | The earlier synthetic-board arena, frozen as a regression test. |

Findings and the shipped values: `docs/design/2026-10-18-heat-autoresearch.md`.

## Development

```bash
otto ci          # whitespace + format + lint + typecheck + test
uv run pytest    # tests only
```
