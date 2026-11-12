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
Replies within the inclusive followup window are marked followup; edits are
always followups, matching existing ledger behavior. File inbox records derive
kind=followup from the ledger marker. Storm transitions send their triggering
message id to the human queue adapters.

Human-pending messages overwrite state/human_queue.jsonl with ids and suggested
lanes only. Adapters with purpose=human_queue receive lane="human_queue" and
message ids. With no such adapter the pipeline logs once and continues exporting.
The timer sweep logs its own missing-adapter warning once. Adapter selection is
independent of kind. To use a session_message adapter for the human queue,
configure and claim a lane named human_queue; its pause control applies. Global
wake controls do not suppress human-queue notices.

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
inbox.root remain independent. Inside the service, state_dir also governs the
wake switch; standalone wake_policy.state_dir behavior is unchanged.

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
observations. One error can be observed at multiple boundaries. Timers have
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
retried on the next sweep without repeating successful adapters. Session notices
blocked by wake.off, pause or an absent claim do not consume receipts.

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
   existing inbox CLI. Include a human_queue lane if its adapter uses sessions.
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
Backfill (origin reconcile) reaches file inboxes only and never wakes a session;
see INGEST.md.
