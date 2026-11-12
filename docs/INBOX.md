# Lane inbox and notifications

Run `python -m slack_dispatch.inbox.cli --ledger dummy.db --inbox-root inbox COMMAND`.
The installed entry point is `slack-dispatch`. Options can appear before or after
COMMAND. `SLACK_DISPATCH_LEDGER` and `SLACK_DISPATCH_INBOX` supply path defaults.
The default inbox root is `inbox`; a ledger path is required without an injected
store. This packet consumes the LedgerStore contract. The bundled ledger remains
a scaffold and must be supplied by the ledger packet before real CLI operation.

## Commands

- `inbox support`: list id, delivery age in seconds, channel, thread flag,
  followup flag and current ledger state. `--include-done` includes completed work.
- `ack slack:CDUMMY:1`: mark the message acknowledged.
- `done slack:CDUMMY:1 --note complete`: complete the message with an optional note.
- `reroute slack:CDUMMY:1 --to content`: route with reason `reroute:<old lane>`
  and append a `reroute` record to the destination inbox.
- `claim support --session dummy-session`: replace the lane's session pointer.
  Without `--session`, use `SLACK_DISPATCH_SESSION`, then `CLAUDE_SESSION_ID`.
- `queue`: show human-pending ids and classifier suggestions.
- `assign slack:CDUMMY:1 support --note reviewed`: record a human assignment with
  reason `human:cli`, optionally save a note, and append to the destination inbox.

A session first claims its stable lane, then reads its inbox. An operator reads
`queue` and explicitly uses `assign` to accept or override a suggestion. A suggestion
alone never changes ownership. Old inbox entries are hidden after rerouting.
Missing messages or destination lanes return 3, invalid input returns 2, and success returns 0.
Unexpected errors return 1 with one sanitized error line and no traceback.

Body output requires `--show-body` on `inbox` or `queue` and a stored body.
The store is called with `include_body=True` only for that explicit request.
Default listings, durable inbox records and notifications never include bodies.
Header lines label tables; data lines begin with ids. Notes are not echoed.

## Durable formats and locking

Each `<root>/<lane>.jsonl` line has exactly `id`, `delivered_at`, `channel`, `ts`,
`thread_ts`, `followup`, and `kind`. Kinds are `new`, `followup`, and `reroute`.
Records are append-only and may repeat ids. Read joins ledger state when a store
is supplied, hides completed work by default, and hides obsolete lane ownership.
Without a store, ledger state is unknown and completion cannot be inferred.
The age column measures time since inbox delivery, not original message age.

Writers hold `<lane>.lock`; readers use it only when it already exists.
Reading without a lock file creates neither a lock file nor a directory. Windows uses `msvcrt.locking`; POSIX uses
`fcntl.flock`. If the native module is unavailable, exclusive atomic creation
retries for up to 30 seconds. The fallback releases by renaming the lock to
`<lane>.lock.released`. A crashed fallback owner leaves a lock requiring operator
inspection and a move aside. Native and fallback lock modes must not be mixed
for the same inbox. Lane names allow lowercase letters, digits, underscores and
hyphens. Appends flush and fsync before releasing the lock.

`FileInboxNotifier(root, lookup=store.get_message)` preserves thread and followup
metadata. Without lookup it derives channel and timestamp from the message id.
`FileDropNotifier(outbox_dir, template)` atomically renames a flushed temporary
file to `<utc>_<uuid>.json`. Exact `{ids}` and `{count}` placeholders preserve
list and integer types; nested templates are supported. `{lane}`, `{title}` and
`{body}` are generated notification metadata, never the source message body.
`assistant_dm.AssistantDMNotifier` is a compatibility alias for FileDropNotifier.

Session command templates should be argument lists with `{target}` and `{message}`
placeholders. No shell is used. Missing or paused sessions are skipped and logged.
Failures retry once and then raise NotifyError without captured output. Dry-run
logs a sanitized rendered command, masking literal arguments and target values.
Templates and session pointers must be credential-free. The sample uses dummy
arguments and dry-run; building adapters does not execute commands.

## Wake policy

| Condition | Session decision |
| --- | --- |
| First event for a lane and author | Deliver immediately |
| Same lane and author within 90 seconds | Defer to the prior delivery plus 90 seconds |
| Immediate user or owner mention | Bypass debounce |
| 30 events in a rolling hour | Enter lane batching, emit StormNotice |
| Lane storm active | Deliver pending ids every 600 seconds |
| Rolling count falls below threshold | Recover normal immediate/debounce behavior |
| Disabled policy or state/wake.off | Block session wakes; retain durable inbox entries |

Thresholds are configurable; there are no daily caps or quiet hours. Immediate
triggers bypass debounce, not storm protection or the global switch. The threshold
event enters batching. Events exactly one hour old leave the rolling window.
Debounce uses a fixed window from the last delivery, avoiding indefinite delay.
Owner mentions can be normalized `mentions` ids or an exact `<@ID>` token in an
in-memory `text` value; text is never retained in the engine's pending queue.

The engine accepts a clock callable returning seconds and exposes Deliver, Defer,
and Batch decisions. A transition's Batch includes a StormNotice, also available
in `engine.notices`; the caller is responsible for consuming operator notices.
`flush(now)` returns due `(lane, ids)` batches. Call it periodically, including
when no new events arrive. `wake_policy.state_dir` defaults to `state` and is
shared by the policy engine and adapter builder. The global switch is
checked at offer, flush, and builder-created session target resolution.
Disabled offers do not enqueue wakes for replay; their durable inbox records
remain available. Already pending wakes are retained while disabled.
Storm counters continue accumulating events while the global switch is off.

Integration order is durable file inbox append first, then wake policy evaluation,
then session delivery for Deliver or due flush batches. File drop is an independent
operator adapter. This packet does not edit service.py or implement a scheduler.
Wake state is in memory; durable pending work remains in the ledger and inbox.
Route mutation and inbox append are separate operations; append failures surface
and require caller retry. Delivery deduplication and crash recovery orchestration
remain service integration responsibilities.
