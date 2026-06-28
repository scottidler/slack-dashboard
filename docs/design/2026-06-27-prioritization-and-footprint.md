# Design Document: Prioritization & Footprint Reduction (Triage v3)

**Author:** Scott Idler
**Date:** 2026-06-27
**Status:** Approved - ready for implementation (Design Review complete 2026-06-27; author decisions locked)
**Builds on:** `2026-06-21-ranking-and-triage-redesign.md` (Implemented, v2)

## Summary

Triage v2 made the dashboard trustworthy: it shows *everything* live (zero-miss) through
density rather than filtering. That worked - and exposed the next problem. With 22 channels,
all currently weighted equally (no `channel-weights` set), the channel-grouped view is now
**three full pages of scroll**. The tool catches everything but no longer *prioritizes*
anything, and the resting footprint is too large to scan at a glance.

This design attacks the problem on two independent axes, because they use different
mechanisms and we want both:

- **Axis A - Prioritize upward (the heat *score*).** Make the right threads float to the
  top: activate channel weights, add **people weights** (a thread is more important based on
  *who* is in it, not just how many), keep velocity.
- **Axis B - Shrink the footprint (what is *rendered at rest*).** Make the default pane one
  screen, not three, *without* breaking zero-miss: progressive disclosure of rows (the same
  trick already used for hover summaries), collapsible groups, cold-tier suppression.

Plus a small cross-cutting idea: **a richer emoji state channel** - more single-glyph
signals that convey meaning in near-zero space.

This is explicitly a request for review *before* implementation. The open questions at the
end are the points we most want the panel to weigh in on.

## Problem Statement

### Background

v2 shipped and is in daily use. The live config (`~/.config/slack-dashboard/`) monitors 22
channels with a global `min-replies: 3` and `max-thread-age-days: 3`, and - critically -
**no `channel-weights`, no per-channel `channel-min-replies`, velocity off**. So every channel
is weight `1.0` and every 3+-reply thread from the last 3 days is rendered.

### Problem

Two gaps, mapping to the two axes:

1. **Nothing is prioritized.** Heat today is `(replies*2 + participants*3) * channel_weight *
   recency`, with `channel_weight` uniformly `1.0`. A `#it-helpdesk` "WiFi outage resolved"
   thread ranks the same as a `#sre` production-node outage with the same raw counts. The
   score has no notion of *which channel matters* (config exists, unused) and no notion of
   *who is in the thread*. The latter is the bigger miss: in the Slack thread that kicked
   this off, the question was literally "does it surface anything me or all Platform members
   are active in?" - and today the honest answer is *no*. Involvement of important people is
   not a signal at all.
2. **The resting footprint is too large.** Because of the zero-miss invariant (no row cap)
   and channel grouping rendering every thread in every group, the default view is ~3 pages.
   Density alone is no longer enough; we need the *important* subset on screen at rest with
   the long tail one interaction away.

### Goals

- Put the threads that matter most (by channel **and** by who is in them) at the top.
- Reduce the **resting** footprint to roughly one screen, while preserving the v2 zero-miss
  invariant: nothing trackable becomes *unreachable*, only *not-rendered-until-asked-for*.
- Keep every ranking signal simple arithmetic and **explainable** - a wrong rank must be
  hand-tunable from config, consistent with v2.
- Add expressive, low-space state signals (emoji) so *why* a row ranks high is visible.

### Non-Goals (inherited from v2, still in force)

- **No @-mention ranking.** Threads Scott is tagged in are Slack's job; this tool is for the
  un-tagged threads he should still know about.
- **No auto-demote-on-read.** Removal stays explicit (Dismiss).
- **No change to the Socket Mode + REST architecture.**
- **Zero-miss is not being weakened.** Footprint reduction must be disclosure, not dropping.

## Proposed Solution

### Axis A: Prioritize upward (heat score)

Current (`heat.py:55`):

```
base  = reply_count*reply_weight(2) + participant_count*participant_weight(3)
score = channel_weight * (base + velocity*velocity_weight) * recency
```

**A1. Activate channel weights.** Already in the schema (`config.HeatConfig.channel_weights`,
glob-aware via `resolve_channel_weight`). Purely a config change: upweight `sre` /
`data-platform` / `incidents`, downweight noisy channels. No code change. Included here only
because it is half of "prioritize."

**A2. People weights (new).** Today every participant contributes a flat `participant_weight`.
Replace the participant term with a *sum of per-person weights*:

```
base = reply_count*reply_weight + sum(person_weight(p) for p in participants)
```

where `person_weight(p)` defaults to `participant_weight` (so behavior is unchanged when no
person is special) but can be raised for specific people. A thread your manager, a VP, or an
incident commander is in outranks one with three random participants at equal volume.

This is the signal that makes "anything important people are active in" real - the exact
question the tool currently cannot answer.

*Source of weights (decided direction: hybrid):*
- **Persona baseline.** Derive a default weight from Tatari's Persona API by org proximity -
  e.g. direct reports and skip-level, leadership, the user's own reporting chain get a boost.
  Avoids hand-maintaining a name list as the org changes.
- **Config override on top.** A `people-weights` map (name or pattern -> weight) in
  `slack-dashboard.yml`, mirroring `channel-weights`, wins over the Persona baseline for
  hand-tuning.

Open design points the panel should pull on: Persona is an external runtime dependency (the
rest of ranking is self-contained and rebuildable from Slack); how do we resolve a Slack
participant (resolved display name today) to a Persona identity reliably; what is the refresh
cadence / caching story; and what is the failure mode when Persona is unreachable (fall back
to config-only + flat default, presumably).

**A3. Velocity.** Unchanged mechanism; still off by default. Turn on once weights settle.

### Axis B: Shrink the footprint (rendered at rest)

The reconciliation principle: **"rendered at rest" must stop meaning "everything."** Zero-miss
requires everything be *reachable*, not everything be *on screen*. This is the same move v2
already made for AI summaries and the per-channel thread list (both hover-only). We extend it
from detail to rows.

**B1. Flat top-N heat view as the default.** Grouping is what explodes height (every channel
section renders all its threads). A flat, ungrouped "top N by heat" pane is the densest
possible "what do I need to know right now." Groups become an opt-in mode, not the default.

**B2. Collapsible channel groups - reusing existing infrastructure.** The `/channel/{id}`
route (`web.py:235`) already renders a channel's full ranked thread list as a hover popover.
A collapsed channel view is therefore nearly free: render 22 one-line headers
(`#it-helpdesk (18) - hottest: <title>`), each expanding the popover that already exists. 22
channels collapsed is one screen; expand-on-interaction reaches everything. Zero-miss intact.

**B3. Cold-tier suppression.** `classify_tier` already buckets hot/warm/cold. Render hot+warm
at rest; collapse the cold tail behind a `+N more` disclosure. With real weights, the cold
tail is exactly the stuff that should not be eating screen space.

**B4. Per-group caps (when grouping).** "Top 5 per channel, `+N more`" for the grouped modes,
same disclosure pattern.

These compose: B1 as the new default; B2/B3/B4 as the grouped-mode behaviors. All four are
template/view-layer work (plus a tiny bit of route support for counts); none touch the heat
math or the poller.

### Cross-cutting: a richer emoji state channel

v2 established emoji as a high-density state channel (`web.py:_emojis`): today just `:zombie:`
(resurrected) and `:fire:` (hot tier). Emoji cost ~one character of width and convey state at
a glance, so there is room to say more. Candidate additions (panel to help prune - too many
glyphs becomes noise):

- **VIP present** (e.g. a crown/star) - a high people-weight participant is in the thread.
  Directly visualizes *why* A2 floated it up.
- **Spiking** (e.g. high-voltage) - velocity above a threshold; "happening right now."
- **New** - thread first seen very recently (distinct from resurrected).
- **Unanswered** - a question with no substantive reply yet (harder to detect reliably;
  flagged as a stretch / may be infeasible without NLP).

Design constraints for the panel to hold us to: each glyph must be unambiguous, must not
duplicate the `Nr|Np` counts, and the *set* must stay small enough to read instantly. An
explicit legend may be warranted once there are more than ~3 glyphs.

## Architecture / Blast Radius

- `config.py` - new `people_weights` map; possibly Persona connection settings.
- `heat.py` - `compute_heat` participant term becomes a per-person sum; new `person_weight`
  resolver (mirrors `resolve_channel_weight`).
- New module for Persona integration (org-proximity -> baseline weights), with caching and a
  graceful-degradation path. This is the only piece adding an external runtime dependency.
- Participant -> identity resolution: today `ThreadEntry.participants` is keyed by resolved
  display name; mapping that to a Persona record is an open risk (see questions).
- `web.py` + templates - new default flat view, collapse/disclosure modes, per-group caps,
  expanded `_emojis`. Reuses the existing `/channel/{id}` popover for B2.
- `thread.py` - likely no new fields (people come from `participants`; VIP/spiking/new are
  computed at render time like zombie/fire, not stored).

## Open Questions (for the review panel)

1. **People-weights via Persona:** is an external runtime dependency in the ranking path
   worth it, given the rest of ranking is self-contained and Slack-rebuildable? Or start
   config-only and add Persona later behind the same resolver?
2. **Identity resolution:** how robustly can we map a Slack participant (resolved display
   name) to a Persona identity? Is there a stable key (Slack user id -> Persona) we should
   thread through instead of names?
3. **People-weight math:** additive-into-base (proposed) vs. a separate multiplicative term
   like channel weight? Additive keeps "important person" from drowning out a genuinely
   high-volume thread; multiplicative makes one VIP dominate. Which serves the job better?
4. **Default view change:** does flipping the default from grouped to flat-top-N (B1) violate
   any v2 expectation? Is "one screen at rest" the right target, or should the user pick the
   default?
5. **Zero-miss vs. disclosure:** is collapsing cold/tail rows behind `+N more` (B2/B3/B4)
   faithful to the zero-miss invariant, or does "not rendered until clicked" cross a line the
   v2 design intended to forbid?
6. **Emoji set:** which of the proposed glyphs earn their place, and at what point do we owe
   the user a legend?

## Alternatives Considered

- **A hard row cap (top 20, drop the rest).** Rejected - directly violates zero-miss. The
  whole point of v2 was that nothing live is ever hidden by the tool's judgment. Disclosure
  (collapse + expand) gets the footprint win without the blindness.
- **Filtering by participation (only show threads SRE/Platform are in).** Rejected as a
  *filter* for the same reason; adopted instead as a *ranking* signal (A2) so important-people
  threads rise without non-important ones disappearing.
- **ML/semantic ranking.** Out of scope; violates the "explainable, hand-tunable arithmetic"
  principle from v2.

## Review Panel Findings (2026-06-27)

Both reviewers verified against the live code and the Persona repos. Synthesis below; raw
outputs archived at `/tmp/review-panel/NuOG78ze/{arch,staff}.out`.

### Blockers (must resolve before implementation)

1. **Participant key is incoherent today, and people-weights amplify it.** The three write
   paths disagree on what a participant key is: the socket listener writes the **raw Slack
   `user_id`** (`listener.py:75`) while the full/incremental REST paths write the **resolved
   display name** (`poller.py:266,269`); `ThreadEntry.participants` is an untyped `dict[str,
   int]` (`thread.py:13`). A user active via both paths is already double-counted under two
   keys. Applying `sum(person_weight(p) ...)` turns this latent bug into a correctness
   failure (raw IDs match no config/Persona key; counts inflated). **Prerequisite:** unify the
   participant key across all three write paths (per the v2 State merge contract) to a stable
   internal key (Slack `user_id`) before any people-weight math.

2. **Display name cannot map to Persona; the join key must be email.** Persona is keyed by
   `work_email` / `employee_id` / `github_username` - neither Slack `user_id` nor display
   name is queryable there. Display names are arbitrary, non-unique, mutable. Only viable
   join: Slack `profile.email` -> Persona `work_email`, which requires the `users:read.email`
   scope on the Slack token (today `_resolve_user` discards email, `client.py:43`). Decision
   on Q2: key participants internally by Slack `user_id`, carry email/display as metadata,
   join to Persona by email - never by name.

3. **Persona must never be called in the ranking hot path.** `compute_heat` runs on every
   update and render is synchronous in `/threads`; Persona is HTTP with a 30s timeout. A sync
   Persona call in `person_weight()` would stall fetch throughput and block `/threads`.
   **Hard requirement:** Persona is an asynchronously-refreshed in-memory snapshot;
   `compute_heat` does an O(1) local dict lookup only, with explicit degrade-to-config-only +
   flat-default on Persona failure.

4. **B3 (cold-tier suppression) + B4 (per-group caps) contradict the v2 zero-miss invariant
   as written.** v2 explicitly banned heat-threshold / tier-filter / `[:15]`-style caps at
   rest. The doc must decide and state plainly whether the product promise changes from
   **"all trackable threads are *rendered* at rest"** to **"all trackable threads are
   *reachable* via deterministic disclosure."** If yes: write the stronger disclosure
   contract (visible `+N more` counts, one-click expansion, **server-rendered tail inside
   collapsed `<details>` so everything is in the DOM** - not HTMX-fetch-on-expand, which
   reintroduces failure blindness; plus tests). If no: B3/B4 do not ship. This is the
   design's biggest open hole and is an author/product decision.

### Corrections to the doc's premises

- **B1 is factually stale.** The default view is **already** flat `group-by=none`
  (`web.py:194`, `index.html`; landed in `456d3dd`). So B1 is not "flip grouped -> flat" - it
  is the sharper "flat-*all* -> flat-*top-N* with disclosed tail," which is a zero-miss
  decision (folds into Blocker 4), not a layout preference.
- **B2 "/channel popover reuse is nearly free" is optimistic.** That route is a hover popover
  bound to channel badges only when *not* grouped (`threads.html:18`); hover-only is not an
  adequate disclosure mechanism for zero-miss (no keyboard/tab, fails on mobile). Treat
  collapsed-groups as new view work needing a channel-level model, not a free reuse.

### Resolved questions

- **Q3 people-weight math: ADDITIVE and bounded** (unanimous). A multiplicative VIP term
  lets one Director in a `#random` thread swamp a real incident's volume heat; additive
  treats VIP presence like a burst of replies. Cap the people bonus so a VIP pile-up cannot
  run away. Channel weight remains the single multiplicative context.
- **Q1 Persona dependency: defer.** Start **config-only**, shaping the `person_weight`
  resolver interface so Persona slots in behind it later, once identity-resolution and
  caching are settled. Do not take the external runtime dependency yet.
- **Q6 emoji:** keep `:zombie:` + `:fire:`; add **VIP** (`:crown:`/star) alongside
  people-weights since it visualizes the Axis-A bump; add **Spiking** (`:zap:`) only once
  velocity is actually enabled and made visually distinct from fire; **reject Unanswered**
  (needs NLP); **defer New** until a real `first_observed_at` field exists (today
  `first_seen_ts` is derived from the thread ts and cannot tell "new to us" from "old
  thread"). Owe a legend at 3+ glyphs (tooltip on the group-by control bar).

### Ranked next actions

- **Land now, independent of everything:** A1 activate `channel-weights` (pure config).
- **Must-fix before people-weights:** unify the participant key (Blocker 1).
- **Author decisions required:** the rendered-vs-reachable product promise (Blocker 4); confirm
  config-only-first for people-weights (defer Persona).
- **Cheap wins after the above:** additive+bounded people-weights; VIP emoji + legend.
- **Defer:** Persona runtime source; New + Unanswered emoji.

## Decisions (2026-06-27, author)

1. **Zero-miss invariant evolves from "rendered" to "reachable."** The v2 promise was: every
   trackable thread is *rendered at rest*. v3 promise: every trackable thread is *reachable
   via deterministic disclosure*. Concretely, the disclosure contract (binding, from the
   panel):
   - The full ranked set is **server-rendered into the HTML** at every refresh; the tail is
     hidden with a collapse (CSS), **never** fetched-on-expand. A network/JS failure can
     therefore never make a thread silently disappear; everything is always in the DOM.
   - A **visible count** of hidden rows is always shown, so the user always knows how much is
     below the fold.
   - Reveal is **one deterministic action**, keyboard- and mobile-reachable (not hover-only).
   - This is tested: a test asserts that the count of rows in the rendered HTML equals the
     count of ranked threads (nothing dropped server-side), and that the tail is present in
     the DOM when collapsed.

   **Chosen mechanism: a single global compact/full toggle** (author, 2026-06-27). Rather
   than per-group `+N more` disclosures, one control flips the whole view between "one page
   worth" (top-N by heat) and "show all." The server tags rows past the fold with a class and
   emits the hidden count; compact mode hides the tail via pure CSS; the toggle carries the
   count ("Show all 87"). The toggle state is persisted via the same JS-global + `hx-vals`
   pattern used for group-by, so the 30s poll preserves the selection (do not reintroduce the
   poll-revert bug). This unblocks B1; it subsumes B3/B4 (no separate per-group caps needed in
   v1 - the global toggle is the disclosure).

2. **People-weights are config-only for now.** Ship a `people-weights` map (Slack user id or
   pattern -> weight) in `slack-dashboard.yml`, resolved by a `person_weight()` function that
   mirrors `resolve_channel_weight`. The resolver interface is shaped so a Persona-backed
   baseline can slot in behind it later without touching `compute_heat`. Persona is **not**
   built in this pass.

3. **People-weight math: additive into base, bounded** (per panel). The participant term
   becomes `sum(person_weight(p) for p in participants)`, with the total people bonus capped
   so a VIP pile-up cannot run away. Channel weight stays the only multiplicative term.

4. **Participant key unification is a hard prerequisite** (Blocker 1). All three write paths
   key participants by stable **Slack `user_id`**; display name/email become metadata. This
   lands before people-weight math, and fixes the existing double-count bug on its own merits.

## Implementation Plan (phased)

- **Phase 0 - Activate channel weights (config-only, do now).** [no model - config edit]
  Add a `channel-weights` block to the live `~/.config/slack-dashboard/slack-dashboard.yml`
  (and confirm the example yml documents it). Independent of all code below; gives immediate
  prioritization. Reversible.

- **Phase 1 - Unify participant key.** [sonnet]
  Make all three write paths (`listener.py`, incremental + full fetch in `poller.py`) store
  participants keyed by Slack `user_id`; resolve display name/email as metadata only. Type
  `ThreadEntry.participants` accordingly. Fixes the double-count bug. Tests assert a user
  active via both socket and REST paths is counted once. Prerequisite for Phase 2.

- **Phase 2 - People-weights (config-only) + VIP emoji.** [sonnet]
  `config.py`: `people_weights` map. `heat.py`: `person_weight()` resolver (Persona-ready
  interface) + additive, bounded participant term in `compute_heat`. `web.py`: VIP glyph in
  `_emojis` when a high-weight participant is present; legend tooltip on the group-by bar.
  Tests for the math, the cap, and the resolver fallback.

  **People-weight tier model (mechanism only; concrete names/weights are private - see
  Privacy below).** The final per-person weight combines a **role overlay** and a
  **team/relationship tier**, taking the higher of the two so a senior person on a low-tier
  team still ranks, with an explicit per-person override on top:

  *Role overlay (by seniority, applies regardless of team):*
  - **VP and above:** super-important.
  - **The author's management chain above them:** super-important.
  - **Director and above (generally):** important (baseline bump over an IC).

  *Team / relationship tier (described by role, not name):*
  - **T1 (highest):** the author's own orgs + close partner teams.
  - **T2 (high):** primary-customer orgs (peer leaders' teams).
  - **T3 (secondary A):** a designated secondary org.
  - **T4 (secondary B):** the remainder of that secondary org.
  - **T5 (lower):** all other teams - kept visible (zero-miss) but ranked down.

  *Per-person override (wins over everything):* specific individuals can be pinned to a high
  weight that beats both their team tier and the role overlay. This is the `people-weights`
  map's primary job, and the top of the precedence order:

  ```
  weight(person) = max(explicit_person_override, role_overlay, team_tier, default)
  ```

  Persona expands each org to its members by traversal (`get_team_by_manager`) and supplies
  titles for the role overlay, so the concrete list is generated, not hand-maintained in
  source. Persona join is by email (Blocker 2), not name.

  **Privacy (hard requirement):** `scottidler/slack-dashboard` is a **public** repo. Concrete
  identities and their weights (real names, emails, the explicit override list, the tier
  assignments for actual people) **must never be committed here, nor to any `tatari-tv` repo** -
  ranking named colleagues by importance would be received badly. The repo carries only the
  **generic schema and placeholder examples**; the real `people-weights` data lives only in a
  private location off this repo (the user's private config; final home TBD - candidate is a
  private `scottidler` repo symlinked into `~/.config` via manifest). The example config must
  use obviously-fake names.

- **Phase 3 - Footprint reduction via a compact/full toggle (the middle ground).** [sonnet]
  Default to the compact view (one page worth, top-N by heat); a single global toggle flips to
  the full view (all rows). The full ranked set is always server-rendered; the server tags
  rows past the fold and emits the hidden count; compact mode hides the tail via pure CSS; the
  toggle shows the count ("Show all 87"). Persist the toggle state with the same JS-global +
  `hx-vals` pattern as group-by so the 30s poll does not revert it. Tests per the disclosure
  contract above. (Collapsible channel groups / B2 is follow-on view work, not free popover
  reuse.)

- **Deferred:** Persona runtime source (behind the Phase 2 resolver); Spiking emoji (gated on
  velocity being enabled); New emoji (needs a real `first_observed_at` field); Unanswered
  emoji (needs NLP).
