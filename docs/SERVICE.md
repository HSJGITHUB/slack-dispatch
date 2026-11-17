# Resident service

The service composes the existing packages. It does not replace the inbox CLI.
Run from a prepared Python 3.12+ environment with dependencies already installed.
Paths in configuration are relative to the process working directory, not the
configuration file. Use an operator-owned configuration for live operation.

```sh
python -B -m slack_dispatch --config dummy-config.yaml --once
python -X utf8 -B -m slack_dispatch --config dummy-config.yaml
python -B -m slack_dispatch.service --config dummy-config.yaml --once --dry-run
```

## Loop

```text
load config -> acquire state/service.lock -> configure logging
  -> open/migrate ledger -> register configured lanes
  -> build suggester, adapters, wake engine, pipeline, timers and sources
  -> publish heartbeat immediately
  -> start sources (reconcile starts its first catch-up on its worker thread)
  -> publish heartbeat
  -> repeat every second:
       inspect stop.flag
       every decisions.poll_seconds (with a card dir): apply Jarvis card decisions (P12)
       every 5 seconds: turn pending wake requests into lane wake offers (P12)
       every 30 seconds: flush due wake batches and failed notifications
       every 60 seconds: sweep overdue ledger items
       heartbeat interval: publish counters
  -> stop sources -> flush due work -> archive stop.flag -> final heartbeat
  -> close ledger -> release lock
```

Source callbacks are serialized with pipeline flush and timer work. Every event
is recorded first. Duplicates and non-message events return without routing.
Deletion events and edits with no existing row also return without routing.
Assigned roots create a thread binding when none exists, so replies inherit.
Existing bindings are preserved. Explicit tags and owner mentions use transient
source text. The ledger receives text but persists it only with store_body=true.
No source text is put in service logs, inboxes, queue exports or retry queues.
The existing suggester receives the ordinary body-free ledger read.

File inbox adapters run before wake policy evaluation, including when wakes are
disabled or deferred. Other lane adapters run on Deliver or due flush batches.
A successful file inbox or lane-adapter call records the thread delivery time.
Replies within the inclusive followup window are marked followup. Edits
(message_changed) follow `wake_policy.followup` (P22, see below); an edit that passes
it is a followup as before. File inbox records carry the
marker in `followup`; kind=followup is written only for edited messages (ledger
edited_at, set by message_changed). A recent reply keeps kind new or backfill.

### Edit wakes (P22)

Slack sends `message_changed` for real text edits and also when link previews attach
(the text unchanged), often several times within seconds. Every edit of an existing row
is recorded in the ledger first (body when store_body is on, `edited_at`, `followup=1`).
The pipeline then compares the normalized new text with the stored body before the
update (both in the P24 comparison form, see below; a missing stored body or new text
counts as changed)
and applies `wake_policy.followup`:

```yaml
wake_policy:
  followup: changed_after_ack   # or always, never
```

- `changed_after_ack` (default): the edit goes on to routing, a followup inbox record and
  a wake offer only when the text changed, the item was `acked` or `done` before the
  event (the lane had already read or closed it) and no edit wake was offered for the same
  message id within `service.followup_window_seconds` (inclusive, process memory). Such an
  edit makes an acked or done item `assigned` again; since P24 it stays in its current
  lane instead of being routed again (see below). A text edit of a `human_pending` item is routed again (fix1): when it now
  routes to a lane it is a first assignment (inbox record, wake and attention notice as
  usual, outside the edit counters and window); when it stays `human_pending` only the
  ledger and the queue export change, with no second queue notice. Every other edit (an
  `assigned` item the lane has not read yet, an unfurl, an excluded item, a second
  change inside the window) is recorded only: no routing, no inbox record, no wake
  offer, no human notice, state and lane unchanged.
- `always`: every edit goes on as before P22.
- `never`: edits update the ledger only.
- Under every policy an edit of a `new` item (recorded but never routed, for example
  after a crash) is routed as a first assignment and is not counted as an edit (fix1).

Limitations: an edit of an `assigned`, `acked` or `done` item never changes its lane
or recomputes its attention flag unless it passes the gate (reassign by hand if needed).
With `ledger.store_body: false` no body is stored, so every edit counts as a text change
and an unfurl of an acked or done item still wakes the lane once per window.

Reply followups (a reply within the followup window of a recent delivery) are
unchanged. Each edit recorded without a wake logs one INFO line `Edit recorded without
wake id=<message id>` (no text). Heartbeat counters `edits_ignored` (edits recorded
without a wake) and `edits_woken` (edits that reached a lane wake offer) appear once
either is nonzero.

### Edits never re-route an item with a lane (P24)

Since P24 an edit of an item that had a lane before the event (state `assigned`, `acked`
or `done` with a lane set, items rerouted or assigned by hand included) is never routed
again: lane, route_reason and the thread binding stay as they are, under every policy.
The gate reads the state and lane as `record_event` found them before it applied the
edit (`RecordResult.previous_state`, `previous_lane`). The only edit that does something
for such an item is the P22 wake case (`changed_after_ack`: text changed, state `acked`
or `done`, outside the window; `always`: every edit): the item becomes `assigned` again
in its current lane and gets a followup inbox record and a wake offer there, without
routing (the attention flag and notice still follow the edited text). Every other edit of
such an item, an `EDIT_FIRST` verdict included, is recorded only. Items the service
closed itself (route_reason or note `owner_post`, `prehandled`, `context`) are never
re-delivered by an edit (fix1). The re-delivery hydrates a missing thread root and counts
in the `routed` counter like other routing. `human_pending` items keep the P22 fix1 rule
and `new` items route normally.

The text comparison compares like with like: the stored body (rendered from rich text
blocks when the message had them) and the new text (the edit's blocks when present,
else its raw mrkdwn `text`) are both reduced to one comparison form first: link and
mention tokens keep their target (`<url|label>` becomes `url`, `<@U...|name>` becomes
`<@U...>`), broadcasts drop, HTML entities decode, formatting markers at word boundaries
(`*bold*`, `_italic_`, `~strike~`, inline code and code fences, line-leading bullets and
quote prefixes) drop and whitespace collapses. Markers inside a word stay, so `5*3` and
`53` or `snake_case` and `snakecase` are different texts (fix1). A link preview edit
that arrives without blocks is therefore no text change. Live case 2026-11-16: a reply
rerouted from lane A to lane B was routed again by a later link preview edit and
landed in lane A as kind `new`: the edit passed the gate and the EDIT_WAKE path
recomputed the lane (thread inherit) instead of keeping the current one.

### Catch-up wakes (P23)

A post made while the service restarts (or while Socket Mode is down) is found by the next
reconcile pass and recorded as backfill (origin `reconcile`); the socket event of the same
message that may follow is a duplicate and does nothing. Backfill normally fills the lane
inbox without a wake, so such a reply waited for the next unrelated wake. Since P23 a
backfill item counts as a live one for waking when its Slack ts is within
`wake_policy.catchup_minutes` of the service clock:

```yaml
wake_policy:
  catchup_minutes: 30   # default; 0 disables catch-up wakes
```

- A reconcile message assigned to a lane with a ts at most `catchup_minutes` away from now
  (inclusive, absolute distance so a slightly early clock also counts) gets its inbox record
  (kind stays `backfill`) and then a wake offer, exactly like a socket message: debounce,
  `immediate_users`, owner mentions, the storm rule and the wake switch apply, and the offer
  counts against the storm budget of the lane. The delivery log row of the wake has kind
  `catchup` (`new` when the woken batch also holds live items).
- Older backfill keeps the old behaviour: inbox record only, no wake offer, no storm
  accounting.
- Human queue items (`human_pending`) and attention notices of backfill items get their
  human notice as before (this did not change). Owner-handled items (`prehandled:*`,
  `owner_post`) are closed and never wake.
- Heartbeat counters `catchup_woken` (reconcile items offered to the wake engine) and
  `backfill_silent` (reconcile items assigned to a lane without a wake offer) appear once
  either is nonzero. The reconcile pass summary line ends with `; catchup_wakes=N`
  (offers made in that pass); `backfill delivered N (no wake)` now counts only the silent
  ones.
- The catch-up marks behind the `catchup` delivery log kind are process memory: a wake
  still waiting in the debounce or storm batch when the service stops is not repeated.

Storm transitions send their triggering message id to the human queue adapters.

Human-pending messages overwrite state/human_queue.jsonl with ids and suggested
lanes only. Adapters with purpose=human_queue receive lane="human_queue" and
message ids. With no such adapter the pipeline logs once and continues exporting.
The timer sweep logs its own missing-adapter warning once. Adapter selection is
independent of kind. A session_message adapter with purpose human_queue addresses
its `template.target` string, or the literal `operator` when unset, as `{target}`.
No lane lookup, claim, session resolution or pause check applies, so no lane named
human_queue is needed; an existing one is ignored for this purpose. Global wake
controls do not suppress human-queue notices; adapter dry_run still applies.
Every human notice (digest or immediate) ends with one line listing the configured
lane ids for `assign ID LANE`, after the command hint.

## Configuration

The service map is optional; each field has the following default:

```yaml
service:
  state_dir: state
  heartbeat_seconds: 30
  log_dir: logs
  log_level: INFO
  once: false
  followup_window_seconds: 600
```

Intervals must be positive integers. once must be boolean. Supported log levels
are DEBUG, INFO, WARNING, ERROR and CRITICAL (case-insensitive). state_dir holds
the lock, heartbeat, control flags and human queue export. ledger.path and
inbox.root remain independent. The wake switch file is `wake_switch_path(config)`:
`<wake_policy.state_dir>/wake.off` when wake_policy.state_dir is set to a value other
than its default `state`, else `<service.state_dir>/wake.off`. The adapter builder,
the delivery gate and (inside the service) the wake policy engine all read that one
path. When the two directories differ the service logs one WARNING at startup naming
the field it uses.

Notify adapters gain purpose, default lane. The other value is human_queue.
The YAML key for is an alias; purpose wins if both are supplied. For example:

```yaml
notify_adapters:
  - kind: file_inbox
    path: inbox
  - kind: file_drop
    purpose: human_queue
    outbox_dir: state/dummy-human-outbox
```

No existing field, required section or default is removed. Configured lanes are
registered before sources start. An existing session claim survives startup;
configured pause settings are applied on startup.

Lane sessions accept a session name, a full session id or an id prefix of at
least 8 hex characters (see INBOX.md, "Session values and precedence"). Id forms
are resolved against the live session files in the optional top-level
`sessions_dir` (default `~/.claude/sessions`, expanded at resolution time):

```yaml
sessions_dir: dummy/sessions
```

Precedence: a non-null `lanes[].session` wins over the ledger pointer written by
`claim`; the ledger pointer applies only when the configured session is null.
An explicit `paused: true` always pauses regardless of the ledger value. Earlier
releases let the ledger pointer win, which kept stale first-launch values after
config edits. The pipeline and timer delivery gate (`can_notify`) applies the same
precedence as the adapter builder, so a configured session wakes its lane even when
the ledger pointer is null, and `paused: true` in config blocks it whatever the
ledger says. The gate also resolves id forms through the shared resolver: an id that
is not live or is ambiguous makes the delivery count as not delivered. The pipeline
does not count it in `delivered` or record a followup delivery, the human queue keeps
its notice pending, and timers write no receipt, so the next sweep retries. Name
lanes pass through unchanged. The adapter's own no-live-session skip stays as a
second guard. When a session_message adapter is configured, heartbeat counters add
`wake_skipped_no_session`: the number of distinct (lane, message id) pairs whose
session wake was skipped because the configured id was not live, per process and
summed over the shared resolver. Retries of the same item do not count again.

Not-live logging is per lane and throttled. The resolver logs on state transitions
only: `session not live: lane X` (INFO) when a lane goes from live or unseen to not
live, `ambiguous session id: lane X matches N live sessions` (WARNING) when it first
becomes ambiguous, and `session live again: lane X` (INFO) when it recovers. While a
lane stays not live or ambiguous it logs at most one reminder line, suffixed
`(reminder)`, every 30 minutes. The 60 second timer sweep therefore no longer logs
one line per item per sweep.

## Once, dry run and exit codes

--once, or service.once=true, starts enabled reconcile sources only. Each runs
one worker-thread catch-up pass, waiting for completion. A service-only subclass closes its interval gate
after that pass, so the inherited interval thread performs no second poll.
The service performs one timer sweep and a due flush before exiting. There is
no local event-inbox format in the source API: "pending" means history after the
ledger's channel cursors. A fresh ledger has no known channels. --once does not start Socket Mode.
With sources[].discover_channels=true, reconcile discovers visible conversations
before the startup pass, including on a fresh ledger.

--dry-run replaces every notifier with an ids-only logging adapter. It does not
call file inbox, file drop or session adapters, write delivery timestamps, or
consume timer receipts. It still records events, routes, exports the human
queue, advances reconcile cursors, writes logs and publishes heartbeats.
Because dry-run does not consume timer receipts, the same notification logs repeat every 60 seconds.
It does not disable source network access or an enabled classifier. Offline
tests inject sources and a disabled/fake classifier; no live source is started.

| Code | Meaning |
| --- | --- |
| 0 | Normal shutdown or successful single pass |
| 1 | Startup/shutdown error, or a single pass with observed failures |
| 2 | Invalid configuration or command arguments |
| 3 | Another process holds the service lock |

Resident loop failures are counted and logged with fixed operation labels,
without exception text or tracebacks. The loop continues. Ctrl+C also performs
orderly shutdown. A failed source stop leaves stop.flag unacknowledged.
Heartbeat counters are cumulative for this process: events includes duplicates
and metadata events; routed counts successful routing applications;
human_pending counts new routing outcomes in that state; delivered counts ids
accepted by successful pipeline delivery groups (inbox and wake groups may both
count the same id); failures includes pipeline, runtime, timer and source error
observations. wake_skipped_no_session (present only with a session_message adapter)
is described under lane sessions above. edits_ignored and edits_woken (P22, present
once either is nonzero) are described under edit wakes above. catchup_woken and
backfill_silent (P23, present once either is nonzero) are described under catch-up wakes
above. One error can be observed at multiple boundaries. Timers have
separate durable receipts and do not increment the pipeline delivered counter.
Heartbeat ok remains false after any observed failure in that process.

## Timers and persistence

A sweep uses received_at for new/human_pending age and assigned_at for assigned
but unacknowledged age, falling back to received_at only if assigned_at is absent.
At the inclusive thresholds it sends an unassigned human notice, a lane reminder,
and a human reassignment proposal respectively. Proposals do not change ownership.
Acknowledged, done and excluded rows are ignored. No message body or reason is
sent. Lane reminders and reassignment proposals skip file_inbox adapters;
session messages carry a reminder prefix and file drops carry a reminder notice.
timer_events identifies the originating timer.

The ledger adds timer_events and human_notices through idempotent v1 migration.
The service also creates service_deliveries through the existing store transaction
lock. P7A upgrades the ledger to schema version 2, preserving attention and
is_bot while adding origin and ingest_gaps. Timer receipts are per item, threshold and adapter configuration
position. Keep adapter order stable for receipt identity. An adapter failure is
retried on the next sweep without repeating successful adapters. Lane session
notices blocked by wake.off, pause or an absent claim do not consume receipts.

Delivery history survives restart. Failed lane notifications are retried on
flush, using ids only. Successful sibling adapters are not repeated for those
retries. Lane retry sets and wake debounce/batch queues are process-local.
Human notification backlogs and receipts are durable in the ledger.
A graceful stop flushes only due work and respects wake.off. Deferred wakes are
not replayed automatically after exit; durable inbox entries remain available.
Exactly-once external delivery is not claimed: a crash between a notification and
its receipt can repeat a timer notice. Event recording, routing, export and
notification are separate commits; a failure after record_event can leave an
item recorded but incompletely processed, and a duplicate callback is skipped.
Inspect the ledger and human queue after failures. These are integration limits,
not changes to the underlying package contracts.

## Operator checklist

1. Run the visibility probe first with the operator's approved configuration.
   Confirm expected channels and event kinds; an empty log is not evidence.
2. Run the offline acceptance suite before preparing a release. Protect the
   configuration, state, ledger, logs and outbox directories.
3. Configure a human queue adapter. Claim the intended lane sessions through the
   existing inbox CLI. A human_queue session adapter needs `template.target`
   (default `operator`); it does not need a human_queue lane.
4. Keep state/wake.off present until source visibility, routing, queue exports,
   lane inboxes and local session delivery have been inspected.
5. Review --once --dry-run with known ledger channels. Remember it still consumes
   source events and updates the ledger. Use an isolated state for a rehearsal.
6. Start the resident service, check a fresh heartbeat and inspect failure counts.
   Move wake.off aside only when ready. Verify real delivery separately.
7. Request stop with state/stop.flag; verify its archive under state/done and the
   final stopped heartbeat. Manage scheduler restart behavior separately.

No live probe, credentials, Slack request, session command or deployment was
used for the service's offline validation. Older architecture/status documents
retain their original packet descriptions; this document describes integration.

## Rehydrate bodies

```sh
python -m slack_dispatch.service --config dummy-config.yaml --rehydrate-bodies 7
```

An exclusive mode: it cannot be combined with --once or --dry-run, starts no source,
loop or notifier, runs one pass and exits. DAYS must be a positive integer. It needs
`ledger.store_body: true`, an existing ledger and a slack_reconcile source (the first
enabled one, else the first), whose token and read client it reuses. Ledger rows
qualify when the message ts is within the last DAYS days, the body is null, the
state is new, assigned, acked or human_pending, and the row was not deleted. Rows in
`excluded_channels` are never fetched. Replies are read with `conversations.replies`
(parent ts, `latest=oldest=ts`, `inclusive=true`), roots with `conversations.history`
using the same bounds. The body is flattened from blocks exactly as the normalizer
does and stored only into a still-null body. Requests are spaced so that at most 40
start in any minute; 429 responses keep the reconcile Retry-After handling. The pass
logs and prints counts only (candidates, fetched, filled, missing, failures,
excluded), never text. Exit codes: 0 success, 1 with fetch or store failures, 2 for
invalid arguments or when body storage, the ledger or a reconcile source is missing.
It does not take the service lock and may run beside the resident service; its
writes are single-row updates through the ledger transaction lock.

## Shadow traffic policies

Human queue notifications default to 30-minute digests. The pipeline and timers
share the same durable backlog. Each adapter emits once an interval is due,
with per-channel unique-message counts, up to 20 ids and queue/assign commands.
Unassigned and reassignment notices join the batch; successful delivery writes
one-shot timer receipts even for ids beyond the preview. Immediate attention
bypasses the interval, and failed or dry-run delivery does not consume receipts.
Heartbeat delivered includes human message deliveries per successful adapter;
timer-only delivery does not increment that counter.

always_human collects attention while normal lane rules continue. Assigned work
still reaches its lane inbox and additionally sends an operator FYI. Bot traffic
uses channel/DM defaults or is excluded by default. It never reaches the human
queue or classifier under route_or_exclude or exclude_all. route_all explicitly
restores full routing. See ROUTING.md for precedence and alternative policies.

Reconcile discovery is opt-in. conversations.list uses GET with public_channel,
private_channel, im and mpim types, with pagination. Discovery runs at startup
and on the first catch-up pass at or after six hours. Discovered ids are retained
in memory for that source lifetime and unioned with known ledger channels.
Excluded channels are removed before any history fetch, including known channels.
Failed discovery is retried on the next pass and does not prevent known-channel
scans. No join, invite, message send, or other write API is introduced.

## P7A coverage

`service.gap_alert_seconds` defaults to 300 seconds. Socket transitions enqueue
metadata only; the service loop records disconnect gaps and wakes the reconcile
worker on reconnect. Startup records a gap and each reconcile source starts an
immediate worker-thread pass. The resident loop does not wait for it: heartbeat,
stop flag, flush and timers run during the first pass. Only --once waits for the
pass to finish. A heartbeat is written right after startup, before sources
start. A source without a
reconcile peer still reports connectivity health, but cannot fill the gap.

Service heartbeat adds started_at, last_event_at, last_catchup_at and process-local
gaps. last_event_at changes only for events of origin socket, so backfill cannot
mask a live intake gap. Standalone writer calls without started_at retain their original key set.
Counters add duplicates, prehandled and hydrated. Disconnection beyond the
threshold makes ok=false and status=degraded until reconnection. Existing
failure counters remain cumulative and may independently keep ok=false.
Only a successful full catch-up marks resumed gap rows with catchup_at.

INFO logs include service version/config basename/pid at start, socket
transitions, one record per gap, channel failures (exception class and validated
Slack error code), reconcile counts and stop. Pipeline failures include the
operation and class. Logs never include source text or exception messages.
Existing daily log rotation and redaction are retained.

Optional reconcile source keys: replies=true, replies_lookback_days=lookback_days,
replies_overlap_seconds=120, bound_threads=true, bound_threads_max_age_days=14. The optional owner_signals map
has posts=record (or route), reactions_done=[white_check_mark, heavy_check_mark]
and reactions_ack=[eyes]. The owner is always_human.owner_user. Root hydration
uses the first configured reconcile source and no source when none is configured.
Backfill (origin reconcile) reaches file inboxes only and does not wake a session,
except recent backfill within `wake_policy.catchup_minutes` (P23, see catch-up wakes
above); see INGEST.md.

## P12 cutover and cards

### Dispatch cards

A notify adapter of kind `dispatch_card` (purpose `human_queue` is required) turns
immediate human-queue notices into Jarvis card requests:

```yaml
notify_adapters:
  - kind: dispatch_card
    purpose: human_queue
    dir: dummy/state/dispatch_cards
    only_immediate: true        # default
    workspace_domain: example   # optional; builds permalinks
```

Only attention notices offered for immediate delivery (`human_queue.immediate_flags`)
queue a card; with `only_immediate: false` and `human_queue.mode: immediate` every
per-item notice does. Digests, timer notices and storm notices never produce cards
(the ids-only `notify` of the adapter is a no-op). Each card is
`<dir>/cards/<message_id_safe>.json` in the `dispatch_card/1` shape (CONTRACTS.md),
written as `<dir>/cards/.tmp/<name>.part`, flushed, then renamed, so Jarvis never sees
a partial file. A failed write leaves the human notice unsent, so the next flush
retries it; the `.part` file stays in `.tmp` (nothing is deleted). Other human-queue
adapters keep working alongside; remove either entry to disable it. The card body is
never logged; the log line carries the message id and the lane count only.

### Card decisions

The decision consumer runs in the resident loop when `decisions.dir` or a
dispatch_card adapter dir is configured (not with `--once` or `--dry-run`):

```yaml
decisions:
  dir: null                 # default: the first dispatch_card adapter dir
  poll_seconds: 5
  hmac_key_file: dummy/keys/dispatch_hmac.env
```

Every `poll_seconds` it reads `<dir>/decisions/*.json` (top level only; `.tmp/`,
`done/` and `rejected/` are never read) and verifies each file: the exact
`dispatch_decision/1` keys, a file name equal to the safe message id, the SHA-256 HMAC
over the canonical JSON without `hmac` (key from `hmac_key_file`, one line
`DISPATCH_HMAC_KEY=<64 hex>`), `decided_by == always_human.owner_user`, no earlier
decision for the message in `decisions/done/`, an existing message that is not a
context row or owner post and is not `done` or `excluded`. It then applies the
decision through the console action code: a lane id assigns with reason `human:card`
(inbox append and a wake request, exactly like a console assign), `direct` assigns to
`console.direct_lane` (rejected when that lane is not configured), `exclude` sets
state `excluded` with note `human:card`. Applied files move to `decisions/done/`,
invalid files to `decisions/rejected/` with one WARNING `Card decision rejected
file=<name> reason=<code>`; names never overwrite (`_1`, `_2` suffixes). Unexpected
failures leave the file in place for the next poll and count as failures. Without a
readable key file the consumer logs one WARNING and leaves every decision file where
it is. The key and message text are never logged. Heartbeat counters add
`card_decisions` and `card_decisions_rejected` when the consumer runs.

### Wake requests

Before P12 an assignment made by the CLI or the console appended an inbox record but
nothing woke the lane session until the next unacknowledged-timer reminder. The CLI
(`assign`, `reroute`), the console actions and card decisions now add one row per
delivered message to the ledger table `wake_requests` (`id`, `lane`, `message_id`,
`requested_at`, `source` = `cli`, `console` or `card`, `consumed_at`). Every 5
seconds the loop reads unconsumed rows and calls `wake_engine.offer` once per distinct
(lane, message id), so the socket debounce, storm and wake switch rules apply; a
Deliver decision wakes the lane's session adapters. Only messages still assigned to
that lane are offered. Every row read is marked `consumed_at`, duplicates included.
Socket, reconcile and cutover backfill deliveries create no requests.

## P18 delivery log

Every attempt to reach a lane session or the operator writes one row in the ledger table
`delivery_log` (created by `migrate()`, additive; the schema version stays 2). File inbox
appends are not logged: they are per message and already visible in the ledger.

```yaml
delivery_log:
  enabled: true
  keep_days: 60          # rows older than this are pruned at startup and once a day
  result_patterns:
    delivered: ['"success":true', "in that session's inbox"]
    not_delivered: ['"success":false', "ENOINBOX", "no inbox", "could not be delivered", "not found"]
```

Columns: `id`, `created_at` (UTC ISO with microseconds, so text order is time order),
`purpose` (`lane` or `human_queue`), `lane` (`human_queue` for operator notices), `kind`,
`adapter` (the adapter kind), `target_session_id`, `target_session_name`, `item_count`,
`item_ids` (JSON list of message ids), `status`, `detail`, `cost_usd`, `duration_ms`,
`relay_session_id`, `last_seen_at` and `repeat_count`. For a relay attempt `created_at` is
the moment the relay started, not when it returned, so an ack that arrives while the relay
is still running counts as the receipt.

A skipped attempt whose status and item ids equal the previous row of the same lane (for
example the 30 second flush retrying a wake while the lane stays paused or not live) adds
no row: that row's `last_seen_at` moves to the new attempt and `repeat_count` grows (null
means 1). Any other row in between starts a new row.

- `kind`: `new`, `backfill`, `catchup` or `reroute` for lane wakes (from the route reason
  and the origin of the woken items; `catchup` is recent backfill woken like a live item,
  P23), `reminder` for timer reminders and timer notices, `digest`,
  `card` and `attention` (attention and storm notices) for the operator; an immediate
  ordinary human notice is `new`.
- `status`: `sent`, `failed`, `timeout`, `dry_run` (adapter `dry_run: true` or the service
  `--dry-run`), `skipped_no_session` (no session value, or an id that is not live),
  `skipped_ambiguous` (an id prefix matching several live sessions) and `skipped_paused`
  (lane paused, `wake_policy.enabled: false` or `wake.off`).
- `detail` holds at most 500 characters: the relay result text, the error class with a
  short reason (`CalledProcessError: exit status 1`, `TimeoutExpired: no answer within
  60s`) or the card count. Never a message body or a token; exception text is not used
  because it can carry the rendered command. A relay answer that repeats the rendered
  wake text has that text replaced by `[message]`.

Where rows are written. The pipeline writes the rows of a refused delivery gate (the
skipped statuses), of the service dry run and of adapters that do not log themselves
(`file_drop`, custom adapters). The `session_message` adapter writes its own row because it
holds the relay result, and the `dispatch_card` writer writes one `card` row per attempt.
The pipeline hands both a shared recorder at construction, so timer reminders sent through
the same adapter objects are logged too (kind `reminder`). The timer sweep's own gate skips
are not logged, so a lane that stays not live does not add a row every minute. A failed log
write is logged by class name and never fails a delivery; the same holds when the skip
reason, the wake kind or the relay output cannot be read (a relay whose output cannot be
read is `sent` with `relay output not read | <class>`).

Relay result. The relay runs with `--output-format json`. Its stdout is parsed as one JSON
object (or a JSON list whose last `type: result` object is used): `result`, `is_error`,
`total_cost_usd`, `duration_ms` and `session_id` fill `detail`, `cost_usd`, `duration_ms`
and `relay_session_id`; `is_error: true` makes the row `failed` (the delivery itself is not
retried, as before). Such an attempt is still recorded as a delivery in
`service_deliveries` and the heartbeat `delivered` counter, so only the delivery log shows
it as failed. The result text is classified with `delivery_log.result_patterns`
(case-insensitive substrings; `"success": true` with spaces also matches) and stored as a
prefix of `detail`: `delivered | <text>`, `not_delivered | <text>` or `unknown | <text>`.
When patterns of both lists match, the one found earliest in the text wins, so the
standard success text that mentions what happens if the session "refuses it" stays
delivered. Output that is not JSON is `sent` with `unparsed output | <first 200
characters>`. The older relay answer `DONE` is `sent` with `unknown | DONE`.

Receipt. A one-shot relay cannot see a later refusal by the target session, so receipt is
measured in the ledger: a `sent` lane row is received at the earliest `ack_at` or `done_at`
of its items that is later than the row and was set while the item still belongs to the
row's lane (a later reroute hides it). A `done_at` with a service, operator or cutover note
(`owner_reaction:`, `human:`, `cutover:` and the other system notes) is no receipt.
Without a receipt the row reports its age. Operator rows have no receipt; a `card` row
reports `decision_at` when a card decision assigned an item after the row (route reason
`human:card`).

Pruning runs when the pipeline is constructed (service start) and from the 30 second flush
once a day. The console and the inbox CLI only read the table.

## P27 owner reply closes direct items

The P17 owner reply close (ROUTING.md, "Owner reply close") now also covers the operator's
direct lane. With `owner_resolver.enabled` and `owner_resolver.close_queue_on_owner_reply:
true` (the same switch, no new key), an owner reply in a thread (`always_human.owner_user`,
recorded as `owner_post`; a live socket event or a catch-up/reconcile event alike) sets to
`done` with note `owner_reply` every item of that thread (the root included) with a lower
ts that is:

- `human_pending` (as in P17), or
- in the configured direct lane (`console.direct_lane`, when it is a configured lane) in
  state `assigned` or `acked`.

Items of other lanes are untouched as before (their lane inbox shows `owner replied at`);
items with a higher ts than the reply stay open; context rows and owner posts are never
items. Unsent human notices of the closed items are dropped and the queue export
(`<state_dir>/human_queue.jsonl`) is rewritten. The owner's own root post closes nothing.
The heartbeat counter `owner_reply_closed` counts both kinds. Without a configured direct
lane only `human_pending` items close, as in P17.

The operator session can record its reply with the inbox CLI instead of waiting for the
event: `sent CHANNEL TS --lane <direct lane> --thread ROOT_TS` with the instance config
closes the same items (see CONTRACTS.md, P27). The service sees the event later and finds
nothing left to close.
