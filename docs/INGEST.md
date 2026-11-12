# Intake and ledger

Socket Mode is primary intake. Reconciliation is a startup catch-up pass and a
periodic history check for channels already known to the ledger. Construction
and `build_sources` perform no network or credential reads. The service wiring
is outside this packet; callers open and migrate the store, upsert configured
lanes, build sources, and start/stop them explicitly.

## Configuration and body policy

The YAML `ledger` section requires `path`; `store_body` defaults to false.
`LedgerStore(path)` also defaults to no body storage. Direct callers can opt in
with `LedgerStore(path, store_body=True)`. The source factory propagates the
ledger setting to both the store and sources. The normalizer accepts an optional
keyword-only `store_body=False`. Default normalized events contain no `text`
key. Only message text can be retained when enabled; blocks, attachments, file
contents, and raw envelopes are never retained. `events_raw` remains unused.
`get_message(id, include_body=True)` explicitly exposes any retained body;
ordinary gets and lists omit the body field. Disabling storage does not purge
previously retained bodies. A deletion event clears the stored body and records
`note=message_deleted`, without creating a missing message.

Identifiers and timestamps are validated using the probe's regex discipline.
Unsupported event types return None. Invalid metadata is replaced with None;
invalid or absent event IDs do not participate in event-ID deduplication. Events
without a valid channel/timestamp cannot create a message. Callback and SDK
errors are counted or raised with fixed text; raw payloads are never logged.
The caller must also avoid logging normalized events containing opt-in text.

## Storage and identity

Open enables foreign keys and requests WAL (SQLite in-memory databases use their
native memory journal). Migrations and all production mutations use
`BEGIN IMMEDIATE` under a per-store lock. Version 1 includes the scaffold and
additive columns/tables; migration also upgrades an unversioned scaffold.
Repeated migrations are idempotent. Unknown schema versions are rejected.
The database parent directory must already exist; deployment owns its creation.

`message_id = source:channel:ts` is stable. Event IDs deduplicate all event types.
A repeated ordinary message with a different event ID is a duplicate by message
identity. Edits update only an existing row, with `edited_at` and `followup=1`;
they never create a missing row. An edit received before the original message
is recorded as seen but is not replayed later. Reactions retain the latest
add/remove timestamps per (message, user, reaction), including references to
messages not yet received. File-share events with a message timestamp update
`has_files`. Standard `file_shared` events lack a message `ts`, so they usually
cannot update `has_files` on a ledger message. Channel events are recorded as `channel_meta` in `events_seen`.
Body-free source/channel columns in events_seen support reconcile discovery.

Lane upserts preserve fields passed as None. Routes and bindings require an
existing lane. State setters accept the six contracted states, timestamp entry
into assigned/acked/done, and preserve prior timestamps. Suggestion reasons
occupy `suggestion_reason`; suggestions never assign a lane. Message lists use
an inclusive `received_at >= since` filter and stable ascending time/ID order.
A missing message mutation is a no-op. `list_reactions` includes removed entries.

## Sources and recovery

Each Socket envelope is acknowledged before filtering or handling. SDK reconnects
are retained; observed disconnect transitions are counted, with recovery resetting
the disconnected state for the next transition.
`start()` connects and returns after starting monitoring; SDK threads deliver
requests. `stop()` closes the client. ACK precedes durable storage, so an event
handler failure can lose real-time delivery; reconciliation covers history
messages but does not reconstruct every event type.

At startup, reconcile attempts one synchronous pass before starting the interval
thread. A startup catch-up failure increments `failures` and still starts the
interval loop. Channel failures increment `failures` and do not skip other channels. It reads every channel from the additive `store.list_channels(source)`
helper: channel metadata, messages, and cursors contribute known channels. A
fresh empty ledger has no channels to query; Socket events or explicit cursor
seeding establish discovery. It does not enumerate the workspace.

Without a cursor, history starts at now minus `lookback_days`. With a cursor,
`oldest` is that Slack timestamp, exclusive. Every page must succeed before
callbacks run in timestamp order. The cursor advances only after all callbacks
return, to the maximum emitted timestamp; an empty response does not advance it.
Callbacks must commit to the ledger before returning. Failure preserves the old
cursor and permits safe replay. Synthetic reconcile event IDs are deterministic
hashes of source/channel/timestamp. Decimal comparison preserves timestamp
precision. Poll intervals use `interval_seconds`, falling back to the existing
`reconcile_seconds` configuration field.

Only `conversations.history`, `conversations.replies`, `conversations.info`, and
`auth.test` pass the call-time allowlist; only history is currently invoked.
HTTP 429 uses `Retry-After` and an injectable, interruptible sleep. A stopped
source does not retry. Other request failures leave cursors unchanged.
History polling does not recover edits to older messages, reactions, deletions,
or replies absent from history under old roots. Thread reply backfill remains
outside this implementation; live visibility must be measured before rollout.

Credentials are loaded only at source startup. The parser handles KEY=VALUE,
CRLF, blank lines, and hash comments, without interpolation or quote stripping.
App tokens require `xapp-`, user tokens require `xoxp-`. Diagnostics contain only
prefix, length, and an eight-character SHA-256 fingerprint. SDK logging is
silenced through a dedicated logger. Socket construction supplies no WebClient
token. Tests mock credential loading and use only synthetic values in memory.

## Probe and verification

Before deployment, follow `tools/probe/README.md` and run the probe with an
operator-provided credential file and a protected output directory. Confirm
visibility for expected event types and channels; an empty log is not evidence
of success. This packet does not run the live probe or access credentials.

From the repository root, run `python -m unittest discover -s tests -v`.
Set `PYTHONDONTWRITEBYTECODE=1` to avoid bytecode files. If installed, run
`ruff check .` with `RUFF_NO_CACHE=1`. Tests use in-memory SQLite, synthetic JSON
fixtures, and fake SDK clients. No network or real credential files are needed.
