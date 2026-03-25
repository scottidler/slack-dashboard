# Design Document: Slack API Throughput Optimization

**Author:** Scott Idler
**Date:** 2026-03-25
**Status:** Implemented
**Review Passes Completed:** 4/4

## Summary

Triple the effective Slack API throughput by exploiting per-method rate limits with separate semaphores, increasing page sizes to reduce total calls, and adding incremental fetching so refreshes only retrieve new messages.

## Problem Statement

### Background

The slack-dashboard fetches data from 22 Slack channels using `conversations.history` (channel-level) and `conversations.replies` (thread-level) REST API calls. After the Socket Mode redesign, the REST fetcher handles initial backfill and thread enrichment via a priority queue.

### Problem

1. **Single semaphore bottleneck.** All API calls share one semaphore with a 1.2s sleep. Slack rate limits are per-method (50 req/min each for history and replies), but we serialize everything as if they share a budget. We use half the available throughput.
2. **Undersized page limits.** `conversations.history` uses `limit=100` (max 200). `conversations.replies` uses `limit=200` (max 1000). More pages means more API calls for the same data.
3. **Full re-fetch on refresh.** Every periodic refresh re-fetches full channel history and full thread replies, even when nothing changed. The `oldest` parameter could skip already-seen messages.

### Goals

- Parallel history and replies fetching using independent semaphores
- Maximize page sizes to minimize total API calls
- Incremental fetching on refreshes using `oldest` parameter
- No increase in 429 error rate

### Non-Goals

- Changing the priority queue architecture (already solid from Socket Mode redesign)
- Adding new API endpoints or Slack app configuration
- Modifying Socket Mode event handling

## Proposed Solution

### Overview

Three independent changes to `SlackClient` and `SlackPoller`:

1. **Per-method semaphores** - Replace the single `_semaphore` with `_history_semaphore` and `_replies_semaphore`, each allowing ~50 req/min (~1.2s pacing). History and replies calls proceed in parallel.
2. **Increased page sizes** - Bump `conversations.history` to `limit=200` and `conversations.replies` to `limit=1000`.
3. **Incremental fetching** - Track the latest `ts` seen per channel and per thread. On refresh fetches, pass `oldest=<last_seen_ts>` to only retrieve new messages. Merge new data into existing thread entries instead of replacing them.

### Architecture

```
SlackClient
  _history_semaphore (1.2s pacing)  -->  conversations.history, conversations.list
  _replies_semaphore (1.2s pacing)  -->  conversations.replies
```

The poller's `_fetch_channel` calls `fetch_threads` (history) then loops through threads calling `fetch_replies` per thread. Within a single `_fetch_channel` call, this is still sequential. The throughput gain comes from the fact that the single queue consumer processes items back-to-back: while a channel-level fetch (history semaphore) is sleeping 1.2s, a thread-level fetch (replies semaphore) from a previous queue item can proceed simultaneously. The two semaphores allow interleaving of history and replies calls across different queue items.

**Data flow change for incremental fetching:**

```
Channel fetch (refresh):
  1. Call conversations.history with oldest=<last_seen_ts>
  2. Only new messages returned
  3. For threads with new activity, queue reply enrichment
  4. Reply enrichment uses oldest=<last_reply_ts> for that thread
  5. Merge new replies into existing ThreadEntry (append participants, update count)
```

### Data Model

**New fields on SlackClient:**

| Field | Type | Description |
|-------|------|-------------|
| _history_semaphore | asyncio.Semaphore | Paces conversations.history calls |
| _replies_semaphore | asyncio.Semaphore | Paces conversations.replies calls |

**New tracking state on SlackPoller:**

| Field | Type | Description |
|-------|------|-------------|
| _channel_watermarks | dict[str, str] | channel_id -> latest message ts seen |
| _thread_watermarks | dict[tuple[str, str], str] | (channel_id, thread_ts) -> latest reply ts seen |

### Implementation Plan

**Phase 1: Per-method semaphores, page sizes, and concurrent processing**
- Replace the generic `SlackClient._call()` with two paced helpers: `_call_history()` and `_call_replies()`, each with its own semaphore and 1.2s sleep
- Route `conversations_history` and `conversations_list` through `_call_history()`
- Route `conversations_replies` through `_call_replies()`
- Bump `fetch_threads` limit from 100 to 200
- Bump `fetch_replies` limit from 200 to 1000
- Remove the 1.0s `asyncio.sleep` between reply fetches in `_fetch_channel` (the replies semaphore handles pacing)
- **Critical:** Change `_consume_loop` to spawn `_process_item` as a fire-and-forget task instead of awaiting it inline. Without this, the single consumer blocks on each item and the dual semaphores never overlap. Cap concurrent tasks (e.g., 10) to avoid unbounded growth.

**Phase 2: Incremental fetching**
- Add `_channel_watermarks` and `_thread_watermarks` dicts to `SlackPoller`
- After fetching a channel, record the latest message `ts` as the channel watermark
- After fetching replies, record the latest reply `ts` as the thread watermark
- Add `oldest` parameter to `SlackClient.fetch_threads()` and `SlackClient.fetch_replies()`
- On refresh fetches (priority=PRIORITY_REFRESH), pass `oldest` from watermarks
- On backfill fetches (priority=PRIORITY_BACKFILL), skip watermarks (full fetch)
- On socket-event fetches (priority=PRIORITY_SOCKET_EVENT), always do full thread fetch (need complete reply list for LLM titling/summarization)
- **Merge logic for incremental reply updates:**
  - Add new participants to existing `participants` set (union)
  - Set `reply_count` to existing count + len(new replies) (not total from API, since we only got a partial response)
  - Update `last_activity` to max of existing and new latest ts
  - Preserve existing `title`, `title_watermark`, `summary`, `summary_watermark`
- Note: `oldest` parameter in Slack API is exclusive (messages strictly after the timestamp), so no duplicate processing

## Alternatives Considered

### Alternative 1: Multiple queue consumers

- **Description:** Run N consumer tasks pulling from the same priority queue, each with its own pacing.
- **Pros:** More parallelism across all call types.
- **Cons:** Harder to reason about rate limits. Risk of exceeding per-method budgets if consumers mix call types.
- **Why not chosen:** Per-method semaphores achieve the same throughput gain with simpler reasoning about rate limits.

### Alternative 2: Header-based adaptive pacing

- **Description:** Read `X-RateLimit-Remaining` from response headers and dynamically adjust sleep duration instead of fixed 1.2s.
- **Pros:** Optimal throughput - never sleep more than needed.
- **Cons:** More complex. The slack-sdk retry handler already handles 429s. Fixed pacing is simple and reliable.
- **Why not chosen:** Marginal gain over fixed pacing for significantly more complexity. Could be added later if needed.

### Alternative 3: Search API for change detection

- **Description:** Use `search.messages` to find recent activity across channels, then only fetch those.
- **Pros:** Single call to find all active channels.
- **Cons:** Tier 2 (20 req/min). Results are less structured. Doesn't return thread reply data. Requires different token scopes.
- **Why not chosen:** Worse rate limit tier and doesn't provide the data we need.

## Technical Considerations

### Dependencies

- No new dependencies. All changes are internal to `SlackClient` and `SlackPoller`.

### Performance

Current state (22 channels, ~50 active threads):
- Single semaphore: ~1 call per 1.2s = 50 calls/min total
- Initial backfill: 22 history + ~50 replies = ~72 calls = ~86 seconds

After optimization:
- Dual semaphores: ~50 history/min + ~50 replies/min = 100 calls/min total
- History calls no longer block replies and vice versa
- Initial backfill: 22 history calls at 1.2s + 50 replies calls at 1.2s, running in parallel = ~60 seconds (down from ~86)
- Larger pages (200 for history, 1000 for replies) mean most channels/threads need only one page
- Incremental refresh: only fetches channels/threads with new activity since last check, reducing ongoing API usage to near zero when idle

### Security

- No security changes. Same tokens, same scopes.

### Testing Strategy

- **Unit tests:** Verify separate semaphores allow concurrent history + replies calls. Verify incremental fetch passes `oldest` parameter. Verify watermark tracking and updates.
- **Integration tests:** Mock Slack API, verify page size parameters. Verify merge logic for incremental thread updates.
- **Manual testing:** Run against real workspace, monitor for 429 errors in logs.

### Rollout Plan

1. Phase 1 (semaphores + page sizes) - low risk, immediate throughput gain
2. Phase 2 (incremental fetching) - slightly more complex, but reduces ongoing API usage significantly

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Dual semaphores cause unexpected 429s | Low | Low | slack-sdk retry handler catches 429s; can fall back to single semaphore |
| Incremental merge misses edited messages | Medium | Low | Full refresh every N minutes catches edits; Socket Mode events trigger full thread fetch |
| Large page sizes cause slower individual responses | Low | Low | Slack handles large pages fine; timeout on our end is generous |
| Watermark drift after restart | Low | Low | Watermarks are in-memory; restart triggers full backfill anyway |

## Open Questions

- [ ] Should we log throughput metrics (calls/min per method) to verify the optimization is working?

## References

- [Slack rate limits documentation](https://api.slack.com/docs/rate-limits)
- [conversations.history API](https://api.slack.com/methods/conversations.history)
- [conversations.replies API](https://api.slack.com/methods/conversations.replies)
