# Dispatch design

## Scope and status
This document describes the target architecture. Version 0.0.1 supplies interfaces
and configuration, not a running service. Example: one operator running about ten
agent sessions can assign work by stable lane without remembering session IDs.
Outbound automation and team-visible status are outside the initial scope.

## Single intake
Use Socket Mode user-event subscriptions for messages, edits, reactions, file
shares and channel membership changes where permissions allow. Visibility must
be measured before rollout; user-event delivery outside bot membership is an
open integration question. Do not infer success from an empty probe log.

A reconcile poller catches up from the last durable cursor at startup and checks
history periodically. Keep one cursor per channel, not one per session. Treat
delivery as at-least-once: deduplicate event IDs and use source/channel/timestamp
as the fallback message identity. Reconcile gaps after disconnections, including
replies to old roots. Concrete retry, pagination and transaction logic is deferred.

## Ledger and ownership
Each message has one current state: new, assigned, acked, done, human_pending or
excluded. Exclusions remain auditable rather than silently dropping messages.
Store source metadata, author, thread identity, lane, route reason, classifier
suggestion/version, follow-up marker and acknowledgement/completion times.
The draft includes assigned_at to anchor the acknowledgement timer.

The ledger is authoritative for message assignment. A separate work registry may
remain authoritative for thread progress and next actions; it must not become a
second writer of assignment state. Bind thread ownership to a lane in the ledger.
Bodies may eventually reside in the protected ledger for deliberate reading,
but never in wake notifications. Optional raw event retention is disabled by
default and requires a retention policy before implementation. The probe does
not retain bodies at all. SQL is a draft, not a migration guarantee.

## Stable lanes
A lane names work, while its session pointer is replaceable. `claim <lane>` binds
the current session without losing pending work. Human-only work uses an owner
lane. Paused lanes accumulate inbox entries without wake alerts; a daily summary
can report pending counts. Final lane mappings, unique session names and a
measured headless wake test are prerequisites to enabling all lane wakes.

## Routing order
Apply the first match in this order:
1. Thread inheritance from an existing parent binding.
2. Explicit configured tag in the message.
3. Channel default, when a single owner is configured.
4. Person and channel rule.

A multi-lane channel uses an explicit null default. Direct-message defaults are
configured separately. Unmatched roots enter the human queue. A classifier may
attach a lane suggestion and reason but cannot finalize assignment in shadow
mode. Save prompt/taxonomy version information with every suggestion so decisions
can be replayed after rule changes.

Future promotion is per channel, explicitly human-approved and based on reviewed
sample volume and measured agreement, never model self-confidence. Thresholds
are deployment policy, not hard-coded company facts. A promoted assignment needs
a documented reversal window; protected senders and operator mentions still
enter the human queue. Promotion is not implemented in this scaffold.

## Inbox and session interaction
The durable per-lane JSONL inbox contains ledger IDs. A wake includes the lane,
count and IDs only. Sessions explicitly read needed content from the ledger,
triage and draft work, then report through the existing assistant adapter.
They do not independently sweep Slack or rewrite a shared registry.

Commands: `inbox <lane>`, `ack <id>`, `done <id> [--note]`,
`reroute <id> --to <lane>`, `claim <lane>`. All mutations must eventually use
transactional ledger operations; JSONL delivery needs restart-safe idempotence.
A prompt hook can provide a passive unread-count reminder. Closely spaced replies
mark follow-up activity so the session rereads the thread before responding.

## Wake policy and timers
Default policy is immediate delivery, with same-author debounce. No blanket
lane cap, inter-lane staggering or quiet hours is intended. A per-lane hourly
storm threshold switches that lane to batch delivery and alerts the operator.
A global switch disables wakes. Sample configuration illustrates a 90-second
debounce, 30 events per hour storm threshold and ten-minute batch window; these
are example settings, not claims about an existing deployment.

There are two timer families: unassigned age and assigned-but-unacknowledged
age. The latter has reminder and escalation thresholds. The sample uses 30
minutes to the human queue, four hours to remind and 24 hours to suggest
reassignment. Human notifications share the assistant adapter's existing
batching and duplicate-suppression gate.

## Service and security
Run a separate resident process with a single-instance lock, heartbeat, durable
state, orderly source shutdown and recovery. A local session notifier uses a
headless CLI call. Human DMs go through an existing assistant's outbox queue,
not a second independent Slack sender. Deployment registration is deferred.

Keep intake and human-notification credentials separate. The future dispatcher
must enforce a read-method allowlist even if an available credential could write.
Store secrets outside the repository with restrictive OS permissions. Instruction
rules alone are not a filesystem security boundary. The helper prints only a
prefix, length and short hash, never a complete value; input and exceptions must
not disclose secrets. Token rotation remains a human operation.

External text is untrusted data. No message body is injected as a session command.
Every outbound message follows draft -> full-text human approval -> publish, and
publishing must update the ledger's last-outbound record in a later packet.

## Rollout and open questions
Implement ingest/ledger, routing/human queue, then inbox/notify packets. Measure
user-event visibility and local headless delivery before enabling wakes. Observe
shadow suggestions, missed-message counts, overdue unassigned work and wake
volume. Migrate legacy ownership to lane IDs and retire per-session polling.
Reaction-based triage, human buttons, local status UI and per-channel promotion
are later work. Team-visible statuses remain deferred until actual demand and
human approval justify them. No live integration claims are made here.
