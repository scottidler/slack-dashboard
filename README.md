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
atrophy     = 0.5 ** (work_hours_since_last / atrophy-half-life-work-hours)
alive-boost = 1 + alive-weight * f(time_alive) * atrophy     # freshness-gated longevity lift
score       = channel-weight * (base-norm + activity) * atrophy * alive-boost * damping
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
- **involvement damping** - drop-and-rebuild: posting in a thread drops its score
  to `involved-drop`, then each unseen reply after your last post rebuilds it back
  toward 1.0 at rate `involved-rebuild-per-msg`.
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
| `heat.involved-drop` | `0.8` | Score multiplier the moment you post in a thread (`1.0` disables). |
| `heat.involved-rebuild-per-msg` | `0.15` | How fast unseen replies after your post rebuild toward 1.0. |
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

- **Compact rows** - `channel:title  Nr · Np  emoji`, grouped (default by
  channel). Every trackable thread renders - there is no row cap.
- **Group by** - `channel | size | velocity | participants`.
- **Hover** a row for the AI summary; **click** to open the Slack thread.
- **Dismiss** - the small control on each row; one click, permanent.

## Development

```bash
otto ci          # whitespace + format + lint + typecheck + test
uv run pytest    # tests only
```
