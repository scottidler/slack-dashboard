# Design Document: Socket Mode Redesign

**Author:** Scott Idler
**Date:** 2026-03-25
**Status:** Draft
**Review Passes Completed:** 4/4

## Summary

Replace the polling-based Slack data fetcher with a hybrid approach: Socket Mode (WebSocket) for real-time message events, REST API for initial history backfill and thread reply enrichment. Socket Mode events reprioritize the REST fetcher so active conversations load first.

## Problem Statement

### Background

The current slack-dashboard uses a polling loop that fetches `conversations.history` and `conversations.replies` for 22 channels via REST API. With rate limiting (1.2s per call) and 37 pages of channels in the workspace, initial load takes minutes. Ongoing polling is slow and wasteful - most channels have no new activity, but we poll them anyway.

### Problem

1. **Initial load is too slow.** 22 channels with many threads each, fetched serially at 1.2s per API call. A 100+ reply thread in the 19th channel doesn't show up for minutes.
2. **Polling wastes API calls.** At night, nothing changes but we still poll every 30-300 seconds.
3. **No real-time updates.** A new message in a thread won't appear until the next poll cycle for that channel.
4. **No prioritization during fetch.** The initial crawl processes channels alphabetically, not by activity.

### Goals

- Real-time thread updates via Socket Mode (sub-second latency for new messages)
- Initial REST fetch reprioritized by live Socket Mode events
- Zero polling when nothing is changing (naturally quiescent at night)
- Thread reply enrichment via REST only when needed (triggered by Socket Mode events)

### Non-Goals

- Replacing REST API entirely (still needed for history backfill and thread replies)
- Bot functionality (no posting, reacting, or responding)
- Handling DMs or group DMs
- Event persistence across restarts (in-memory is fine)

## Proposed Solution

### Overview

Two concurrent systems feed the same in-memory thread store:

1. **Socket Mode Listener** - Connects via WebSocket, receives `message` events in real-time. When a thread gets a new reply, it queues that thread for REST enrichment. This runs from the moment the app starts.
2. **REST Fetcher** - Processes a priority queue of (channel_id, thread_ts) pairs. Initially seeded with all configured channels for backfill. Socket Mode events jump the queue. When the queue is empty, the fetcher is idle (no polling).

### Architecture

```
Slack WebSocket  -->  Socket Mode Listener  -->  Priority Queue
                                                      |
                                                 REST Fetcher  -->  Thread Store  -->  Web Server
                                                      |
                                              Slack REST API
                                           (conversations.history,
                                            conversations.replies)
```

**Data flow:**

1. On startup, seed the priority queue with all 22 channels for initial history fetch (low priority).
2. Socket Mode connects and begins receiving `message` events immediately.
3. When a Socket Mode event arrives for a thread, update the thread store's metadata (reply count, last activity, participants) directly from the event payload. Then queue a high-priority REST fetch for that thread's full replies.
4. The REST fetcher pulls from the priority queue. High-priority items (Socket Mode triggered) are fetched before low-priority items (initial backfill).
5. When the queue is empty, the fetcher sleeps. No polling loop.
6. Periodically (e.g., every 10 minutes), re-queue channels for a light refresh to catch anything Socket Mode might have missed (reconnection gaps).

### Data Model

**Priority queue item:**

| Field | Type | Description |
|-------|------|-------------|
| channel_id | str | Slack channel ID |
| channel_name | str | Human-readable name |
| thread_ts | str or None | Specific thread to fetch (None = fetch channel history) |
| priority | int | 0 = Socket Mode triggered (highest), 10 = initial backfill, 20 = periodic refresh |

**ThreadEntry** - unchanged from current implementation, same fields.

**New: Socket Mode event metadata applied directly to ThreadEntry:**
- Increment reply_count
- Add user to participants set
- Update last_activity timestamp
- Recompute heat score
- These updates happen instantly, before REST enrichment completes

### Configuration Changes

Add to `~/.config/slack-dashboard/slack-dashboard.yml`:

```yaml
slack:
  token: "${SLACK_USER_TOKEN}"
  app-token: "${SLACK_APP_TOKEN}"
```

The `app-token` is an `xapp-...` token with `connections:write` scope, generated in the Slack App settings under "App-Level Tokens".

Remove the `polling` config section entirely. No more polling intervals.

Add a `refresh-interval-minutes` under a new section if desired:

```yaml
fetch:
  refresh-interval-minutes: 10
  min-replies: 3
```

### Socket Mode Integration

Using `slack_sdk.socket_mode.aiohttp.SocketModeClient`:

- Subscribes to `message.channels` and `message.groups` events
- Filters events to only configured channel IDs
- For each message event:
  - If it has `thread_ts` (is a reply or thread parent): update thread metadata in store, queue high-priority REST fetch for full replies
  - If standalone message (no thread_ts): ignore (we only care about threads with min-replies)
- Must acknowledge every event via `SocketModeResponse(envelope_id=req.envelope_id)`
- Auto-reconnect is enabled by default in the SDK
- Shares the same `AsyncWebClient` instance as the REST fetcher

### REST Fetcher

Replaces the current `SlackPoller._poll_loop()`:

- Pulls items from an `asyncio.PriorityQueue`
- For channel-level fetches (thread_ts=None): calls `conversations.history`, identifies threads with >= min-replies, queues each for reply fetching
- For thread-level fetches (thread_ts set): calls `conversations.replies`, updates full thread data in store
- **Deduplication:** maintains a set of currently-queued (channel_id, thread_ts) pairs. If a thread is already queued, skip it. Cleared after fetch completes.
- Maintains 1.2s pacing between API calls (same semaphore as today)
- When queue is empty, blocks on `queue.get()` (no busy-waiting, no polling)
- Periodic refresh: a background timer re-queues all channels every N minutes at low priority

### Web Frontend Changes

- HTMX polling interval can be longer (60s or more) since data updates are event-driven
- When a Socket Mode event updates a thread, the next HTMX poll picks it up
- Consider adding Server-Sent Events (SSE) later for truly instant frontend updates (future enhancement)

### Slack App Setup Required

In the Slack App settings (api.slack.com):

1. **Enable Socket Mode** - toggle ON in Socket Mode section
2. **Generate App-Level Token** - with `connections:write` scope, save as `SLACK_APP_TOKEN`
3. **Subscribe to Events** - under Event Subscriptions, add:
   - `message.channels` (public channel messages)
   - `message.groups` (private channel messages)
4. **Reinstall app** to workspace after changes

### Implementation Plan

**Phase 1: Priority queue fetcher**
- Replace polling loop with priority queue consumer
- Seed queue with all channels on startup
- Keep existing REST fetch logic, just change the scheduling

**Phase 2: Socket Mode listener**
- Add Socket Mode client alongside REST fetcher
- Wire message events to update thread store + queue REST enrichment
- Add app-token to config

**Phase 3: Remove polling**
- Remove polling config section
- Add periodic refresh timer (low priority re-queue)
- Verify quiescent behavior (idle when no activity)

## Alternatives Considered

### Alternative 1: Events API with HTTP webhooks

- **Description:** Slack POSTs events to a public HTTP endpoint instead of WebSocket.
- **Pros:** Standard approach for production Slack apps.
- **Cons:** Requires public URL or tunnel. More infrastructure. Not suitable for running on a laptop behind NAT.
- **Why not chosen:** Socket Mode works behind firewalls with outbound WebSocket only. Perfect for a personal tool running on a LAN machine.

### Alternative 2: Keep polling, just optimize

- **Description:** Optimize the current poller with smarter batching, parallel fetches, and better interval tuning.
- **Pros:** No new dependencies or Slack App config changes.
- **Cons:** Still wastes API calls. Still has latency. Still rate-limited. Fundamentally the wrong architecture for real-time.
- **Why not chosen:** Polling cannot achieve sub-second updates. The rate limit constraints make it inherently slow.

### Alternative 3: Slack RTM API

- **Description:** Real-Time Messaging API via WebSocket.
- **Pros:** Real-time, similar to Socket Mode.
- **Cons:** Deprecated by Slack. Not available for new apps. Missing modern event types.
- **Why not chosen:** Deprecated.

## Technical Considerations

### Dependencies

- **New:** `slack_sdk.socket_mode.aiohttp.SocketModeClient` (already in slack-sdk, no new package)
- **New config:** `SLACK_APP_TOKEN` environment variable (xapp-... token)
- **Unchanged:** All existing dependencies remain

### Performance

- Socket Mode events arrive in <1 second from when the message is posted
- REST enrichment adds 1-2 seconds per thread (one API call through semaphore)
- Initial backfill speed unchanged (still rate-limited), but active threads jump the queue
- At night with no activity: zero API calls, zero CPU, just an idle WebSocket connection

### Security

- App-level token (xapp-...) grants `connections:write` only - cannot read messages or channels
- User token scopes unchanged
- Socket Mode connection is outbound-only (no inbound ports needed)
- Both tokens stored as environment variables, never in config files

### Testing Strategy

- **Unit tests:** Priority queue ordering, event-to-thread-update logic, queue seeding
- **Integration tests:** Socket Mode event handler with mocked client, REST fetcher with mocked API
- **Manual testing:** Real Slack workspace, post messages, verify they appear on dashboard within seconds

### Rollout Plan

1. Implement priority queue fetcher (no Socket Mode yet, just better scheduling)
2. Add Socket Mode listener, test with real events
3. Remove polling loop, verify quiescent behavior
4. Tune refresh interval based on real usage

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Socket Mode disconnects during event delivery | Low | Low | SDK auto-reconnects; envelope_id prevents duplicate processing; periodic refresh catches gaps |
| App-level token expires or is revoked | Low | High | Health check monitors Socket Mode connectivity; fall back to periodic REST refresh |
| Event volume overwhelms REST enrichment queue | Low | Low | Priority queue bounds depth; semaphore limits API call rate; old items age out |
| Missing events during reconnection window | Medium | Low | Periodic low-priority refresh re-queues all channels every N minutes |

## Heat Algorithm Redesign (bundled with this work)

The current heat formula is broken for evening/off-hours use. Old threads with high reply counts dominate because recency is additive (max 100 points) while reply count is unbounded.

**New formula:** Recency should be a decay multiplier, not an additive bonus. A 200-reply thread from last month should score near zero. A 10-reply thread from today should score high.

```
base = (reply_count * reply-weight) + (participant_count * participant-weight)
decay = max(0.01, 1.0 - (hours_since_last_activity / decay-half-life-hours))
heat = base * decay
```

With `decay-half-life-hours: 24`, a thread goes to ~50% heat after 12 hours, ~1% after 24 hours. Today's threads always dominate.

**Staleness filter:** Threads where `last_activity` is older than `max-thread-age-days` (default: 3) are excluded entirely. No one cares about last month's thread regardless of reply count.

Add to config:
```yaml
heat:
  decay-half-life-hours: 24
  max-thread-age-days: 3
```

## Open Questions

- [ ] Should we add Server-Sent Events (SSE) to push updates to the browser instantly, or is HTMX polling at 60s sufficient?
- [ ] Should the periodic refresh interval be configurable or hardcoded at 10 minutes?
- [ ] When Socket Mode receives a message for a thread below min-replies threshold, should we still track it in case it crosses the threshold soon?
- [ ] Best default for max-thread-age-days - 3 days? 7 days? Configurable.

## References

- [Slack Socket Mode documentation](https://docs.slack.dev/apis/events-api/using-socket-mode/)
- [slack_sdk Socket Mode client](https://docs.slack.dev/tools/python-slack-sdk/socket-mode/)
- [message.channels event reference](https://docs.slack.dev/reference/events/message.channels/)
- [message.groups event reference](https://docs.slack.dev/reference/events/message.groups/)
- [Slack App-Level Tokens](https://api.slack.com/concepts/token-types#app-level)
