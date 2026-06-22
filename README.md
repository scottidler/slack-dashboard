# slack-dashboard

A single, scannable triage pane over the Slack channels you can no longer skim.
It monitors channels in real time (Socket Mode + REST hybrid), heat-ranks threads
you are **not** tagged in, and surfaces them as dense, one-line rows so you can
catch what skimming would have caught - without hiding anything.

The headline guarantee: **stay clean through density, not filtering.** No
trackable live thread is ever hidden by the tool's relevance judgment.

## How ranking works

Each thread gets an explainable, hand-tunable heat score:

```
base      = reply_count * reply-weight + participant_count * participant-weight
velocity  = replies_in_window / velocity-window-minutes
recency   = max(decay-floor, 1.0 - hours_since_last_activity / decay-hours)
score     = channel-weight * (base + velocity * velocity-weight) * recency
```

- **channel-weight** - the highest-leverage knob. Weight the channels you care
  about (`sre`, `data-platform`) up and the noise (`proj-*`) down. Supports glob
  patterns; exact keys win over globs.
- **velocity** - boosts a thread spiking *now* over one that merely accreted
  replies slowly. Defaults to off (`velocity-weight: 0`).
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
| `heat.velocity-weight` | `0.0` | How much a currently-spiking thread is boosted. |
| `heat.velocity-window-minutes` | `30` | Window over which velocity is measured. |
| `heat.decay-hours` | `24` | Linear recency ramp length (renamed from `decay-half-life-hours`, still accepted). |
| `heat.decay-floor` | `0.01` | Minimum recency multiplier for very old threads. |
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
