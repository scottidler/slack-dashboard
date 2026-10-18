# program.md: autoresearch over the heat formula

Karpathy's autoresearch loop (github.com/karpathy/autoresearch) pointed at the heat model.
Three files, same split as upstream:

| upstream     | here                 | who edits it |
|--------------|----------------------|--------------|
| `prepare.py` | `bin/replay.py`      | nobody during a loop: the data, labels, checkpoints and metric |
| `train.py`   | `bin/formula.py`     | the agent, and only this file |
| `program.md` | `bin/program.md`     | Scott |

The corpus is `bin/corpus.py` output (`~/.local/share/slack-dashboard/corpus.jsonl`) plus the
jev topic sidecar from `bin/topics.py` and the judge sidecar from `bin/judge.py`. One run
replays every scheduled look (every work hour, 6 to 17 PT, on work days), scores the board
with `formula.py`, and prints one number per line. What a run found and what shipped:
`docs/design/2026-10-18-heat-autoresearch.md`.

## The metric

`objective:` is the train number: the mean of catch and purity on the involvement label.

- Involvement label: a thread Scott stepped into (replied to, did not start) is *needed* at
  every look before his first reply. Everything else is "not it", including a thread he
  already joined: Slack follows it for him, so the board earns nothing by showing it.
- catch: per needed thread, the best credit over its needed looks, 1 inside the top 10 else
  10/rank, averaged over threads.
- purity: per look, the share of the top 10 that is needed at that look (he joins it after
  the look), averaged over looks.

`train_judge:` and `train_habit:` print the same two numbers on the other labels (jev's
p(needs Scott) >= 0.6; replied-before-the-next-look plus late bloomers). Log them, never
steer by them: a keep that moves the objective and tanks the judge line is a keep that found
a hole in the involvement label, say so in the description.

`holdout:` is the objective on the last 7 days of the corpus. The loop never decides on it.
It is printed so the final verdict can be made on data the loop did not optimize, which is
the one guard the upstream method lacks and every port that skipped it regretted.

## The loop

Run once before anything else and log the live formula as the baseline. The live values are
the yml's, not the ones that happen to be in `KNOBS`; check them against
`uv run python -c "from slack_dashboard.config import load_config; ..."` before run 0. The
first loop here carried reply 1 / participant 5 / activity cap 50 against a yml saying
2 / 3 / 20 and had to re-measure.

```
uv run bin/replay.py
```

Then, forever:

1. Pick one idea. Edit `bin/formula.py` and nothing else.
2. `uv run bin/replay.py > run.log 2>&1` then `grep "^objective:\|^holdout: [0-9]\|^formula_lines:" run.log`.
3. Empty grep = crash. `tail -n 30 run.log`, fix it a couple of times, then give up on the idea.
4. Append one row to `~/.local/share/slack-dashboard/results.tsv`:
   `n  objective  holdout  lines  keep|discard|crash  description` (tabs, no commas needed).
5. If `objective` improved by at least 0.005: keep. Copy the file to
   `~/.local/share/slack-dashboard/formula-best.py`.
   Otherwise: discard. Copy `formula-best.py` back over `bin/formula.py`.

The keep/revert copy replaces upstream's git commit/reset because this worktree is shared with
another session's uncommitted changes; a per-experiment commit here would sweep them up.

Channel weights are an input, not a knob: `--weights some.json` swaps the prior in without
touching the formula, so a prior experiment is one run per candidate, not an edit.

## What you can do

- Change any knob value in `KNOBS`.
- Change the shape of any term: saturation, half-life, logistic, log, clamp, ratio, rank.
- Add a term over the `Features` the harness already provides. Remove a term.

## What you cannot do

- Edit `bin/replay.py`, `bin/explore.py`, `bin/corpus.py`, `bin/topics.py`, `bin/judge.py`,
  the corpus, or the sidecars. The metric is the ground truth; a change to it is not an
  experiment.
- Change who is on the board. `min_replies` and `max_thread_age_days` come from the config
  and the harness overwrites whatever `KNOBS` says: an earlier run "won" 6 points by setting
  max age to 1 day, which moved needed threads off the board instead of catching them.
- Move the following knobs. `followed_drop` and `followed_rebuild_per_msg` are owner policy:
  the harness overwrites them from the yml the same way.
- Add dependencies. Add features that need data the harness does not pass.
- Read or optimize on `holdout:`. Log it, never steer by it.
- Stop. If you are out of ideas, combine the near misses, or try the opposite of a discard.

## Taste

- Simpler wins ties. Equal objective with fewer lines is a keep.
- A 0.003 gain for a new branch of code is a discard even if it clears the threshold on noise.
  The involvement label has 17 joined threads in train (145 needed rows over 180 looks); one
  row is worth about 0.7 points of objective. A change that lifts exactly one of them and
  nothing else is probably noise, say so in the description.
- A kept change should read as a sentence about Scott ("resolved threads leave the board",
  "a thread jev says needs a leader counts as a full-size thread"). If it does not, it is
  curve fitting.
- Followed threads drop by design; the loop cannot move those knobs. A thread Scott started
  or posted in is one Slack already tells him about, so the board is for the others.
- When done, run `--schedule 6 7 8 9 10 11 12 13 14 15 16 17` (the default) and
  `--schedule 9 16` on the final formula: a keep that only works at one schedule is suspect.

## The next loop

The app now appends `events.jsonl` (opens and dismisses, with rank and score at the moment)
beside the config. Once a few weeks of it exist, join it to the corpus by
`(channel_id, thread_ts)` and run the loop against "opened or dismissed" as the label. That
is the signal the involvement label lacks, and the reason the current formula's purity
ceiling (about a quarter of the top 10) is structural rather than a knob.
