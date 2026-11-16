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
`BEGIN IMMEDIATE` under a per-store lock. Version 2 includes the scaffold and
additive columns/tables; migration upgrades version 1 and an unversioned scaffold.
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

At startup, reconcile performs a worker-thread pass. A standalone source
(`wait_for_first_pass = True`) waits for it before start returns; the resident
service source does not wait (see SERVICE.md). A startup catch-up failure increments `failures` and still starts the
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
`auth.test` pass the call-time allowlist; history, replies and optional discovery are invoked.
HTTP 429 uses `Retry-After` and an injectable, interruptible sleep. A stopped
source does not retry. Other request failures leave cursors unchanged.
History polling does not recover edits to older messages, reactions, deletions,
or every historical event. Thread reply backfill is described under P7A coverage.

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

## P7A coverage

Reconcile now fetches roots and replies on its worker thread at startup, after
socket reconnect requests, and periodically. Optional source keys are
`replies: true`, `replies_lookback_days` (defaults to `lookback_days`),
`replies_overlap_seconds: 120`, `bound_threads: true` and
`bound_threads_max_age_days: 14` (positive integer). History pages are
shared across the root and reply lookback windows. Root delivery waits until
candidate reply pages have been fetched, allowing owner-reply evidence
to suppress already handled roots. Root delivery and the root cursor never
depend on the replies step: each parent is fetched in its own guard, a failed
parent is skipped with one WARNING line (channel, parent ts, exception class,
Slack error code) and counted in the pass `failures`, and a root whose replies
could not be fetched routes without reply-based evidence. The separate
`slack_replies` cursor advances to pass start only when every parent of the
channel succeeded and every reply callback returned; otherwise it is kept.
Overlap is deduplicated by deterministic event ids and message identity.

A parent whose replies fetch fails with a permanent Slack error code
(`thread_not_found`, `channel_not_found`, `not_in_channel`, `message_not_found`,
`is_archived`; `PERMANENT_REPLY_ERRORS`) is added to an in-process skip map
(`skipped_parents`, parent -> pass time) and logged once at WARNING with channel,
parent ts and error code. It is not counted in `failures` and does not hold the
`slack_replies` cursor. After `permanent_error_ttl_seconds` (optional reconcile
source key, default 3600) the parent is retried once; a thread the user was invited
into later is then read again without a restart. If the retry fails with a permanent
code again the parent is skipped for another TTL and logged at DEBUG only (the
WARNING is not repeated for that parent). A successful retry removes it from the map;
a transient error on the retry is an ordinary failure. The pass summary counts
skipped parents, new or remembered, as `permanent_skipped`. Other errors (rate
limits, network and unknown codes) keep the behaviour above: they count in
`failures`, keep the cursor and retry next pass.

Reply parents include recent history parents with newer replies, channel
bindings (when `bound_threads` is true) and threads referenced by recent ledger
messages. Binding and ledger candidates are checked only when their thread
activity, max(thread_ts, newest ledger message ts in that thread), is within
`bound_threads_max_age_days` of the pass start. Excluded channels are never
scanned. The pass summary reports `thread_candidates`, `thread_skipped_age` and
`permanent_skipped`.
Replies are paginated and delivered in timestamp order. Origins are `socket`,
`reconcile`, and `hydrate`. Initial inbox delivery from reconcile uses `backfill`;
explicit reroute and followup kinds retain precedence.

Backfill does not wake sessions. A routed message of origin `reconcile` is
written only to `file_inbox` adapters with purpose `lane`; the wake engine
(`offer`) and its storm accounting are skipped, so session_message, file_drop
and assistant_dm lane adapters are not called for it. Human-queue notices for
attention flags are unchanged. The pass summary ends with
`backfill delivered N (no wake)`. Hydrated context rows are never delivered.

With body storage enabled, supported rich-text blocks flatten into text, mentions,
URLs, emoji and line breaks. Unknown elements are skipped; empty supported block
text falls back to top-level text, then file names/titles. Edits use the nested
message object. Raw blocks, file contents and envelopes are never stored.

Missing thread roots are fetched through the reconcile client with a ten-second
wait budget, recorded as done context rows and never routed or delivered.
Successful hydration is cached for the process lifetime; failed or outstanding
requests are retried or collected on a later catch-up pass, at most three
retries per root after the first failure. After that the root is abandoned for
the process lifetime with one INFO line naming the root id. Roots in
`excluded_channels` are never fetched. No reconcile source means no hydration. Reconcile does not reconstruct historic edit/delete events.

Owner signals default to posts=record, reactions_done=[white_check_mark,
heavy_check_mark], reactions_ack=[eyes], under `owner_signals`. See ROUTING.md.
Service gap health uses `service.gap_alert_seconds: 300`; see SERVICE.md.

## P15 member-only intake

A user-scoped Socket Mode connection also delivers events from public channels the
operator is not a member of (for example a workflow posting on a teammate's behalf).
`slack_dispatch/ingest/membership.py` keeps the operator's member channels so those
events never reach routing.

`MembershipCache(client, store, refresh_seconds=3600)` calls `users.conversations` with
types `public_channel,private_channel,mpim,im` (paginated, 200 per page, archived
excluded) and stores the result in the ledger table `channel_membership(channel PK, kind,
name, partner_user, is_member, seen_at)`. `kind` is `public_channel`, `private_channel`,
`mpim` or `im`; `partner_user` is the other user of a DM. A channel no longer listed
keeps its row with `is_member=0`. `client` is a Slack client or a callable returning
one: the service passes the reconcile source's read-only client (`users.conversations`
is on the reconcile allowlist), so no second token is loaded. Construction makes no
request; a refresh without a client is skipped.

Refresh runs at the start of the first reconcile pass after service start, then every
`membership.refresh_seconds` (the reconcile pass and the service loop both ask; one
refresh runs at a time). The previous run's table is loaded at once. A refresh failure
keeps the previous table and in-memory set and logs one WARNING line with the
exception class and Slack error code only. A successful response with no channels
counts as a failure: the previous table and in-memory set stay, with one WARNING
`membership refresh returned no channels; keeping previous table`. The cache becomes
available when it first holds member channels (one INFO line); then the
`channel_defaults` keys (excluded channels skipped) that are not member channels are
reported once per process in one WARNING summary line
`N configured channels are not member channels: id1, id2, ... (first 5)`. Such
channels are treated per policy.

```yaml
membership:
  enabled: true          # default true
  refresh_seconds: 3600  # positive integer
  policy: exclude        # exclude (default) or off
```

Policy `exclude`: an event from a channel that is not a member channel is recorded
(event dedup and the message row as usual), then set to state `excluded` with note
`excluded:not_member`. It is never routed, queued, delivered or counted as
`human_pending`, and no thread root is fetched for it. DM and group DM channels in the
cache are member channels; a DM or group DM not yet in the cache (channel type `im` or
`mpim`, or a `D` id) is also treated as a member, because user-scoped events for direct
conversations reach participants only. While the cache has never loaded (first start
before the first refresh succeeds) every channel keeps today's behaviour. Policy `off`
keeps today's routing; the cache still refreshes for the console badge and discovery.
`enabled: false` builds no cache. Without a reconcile source there is no client, so the
cache never loads and routing is unchanged.

Reconcile reads member channels only: the scan set (ledger channels plus discovered
channels, minus `excluded_channels` and the ledger channel exclusions) drops channels
the policy excludes. With the cache enabled, `discover_channels` takes the member
channels from the cache instead of calling `conversations.list`.

Heartbeat counters gain `member_channels` (member channels in the cache) and
`excluded_not_member` (events excluded by the gate since start, not counting channels
already in the config `excluded_channels`, which stay blocked) when the cache exists.

## P16 user directory

`slack_dispatch/ingest/user_directory.py` decides which authors are bots. A teammate
posting through their own assistant app produces a message with a human `user` plus
`bot_id`, `app_id` and `bot_profile`, exactly like a real bot user; only `users.info`
(`is_bot`) tells them apart. The rule is in ROUTING.md (P16).

`UserDirectory(client, store, ttl_seconds=86400, known_bot_users=())` is modelled on
`MembershipCache`: `is_bot(user)` returns True at once for `known_bot_users`; otherwise
it reads the ledger table `user_directory(user PK, is_bot, is_app_user, deleted, name,
seen_at)` and, on a miss or a row older than `ttl_seconds`, calls `users.info` (added to
the reconcile read-only allowlist `READ_METHODS`) and stores the row. `client` is the
reconcile source's read-only client or a callable returning it; construction makes no
request. The pipeline consults it only for message events carrying `bot_id` or
`app_id`; lookups run under the pipeline lock, at most one per user per TTL.

A lookup failure (exception including HTTP 429, `ok: false`, missing or mismatched
`user` object, no client) writes nothing to the table and logs one WARNING per user per
hour with the user id, exception class and Slack error code only. A cached row older
than the TTL keeps its value (a bot account does not become human); a user with no
cached row counts as human for that event (fail open: a bot post reaching a lane costs
one `done`, a lost human post costs a missed reply). After a failure the user is not
looked up again for `failure_cooldown_seconds` (in memory only; during the cooldown the
cached row's value is used, else human), so an outage does not cost one call per post.

Socket and reconcile sources no longer set `is_bot`: shared normalization keeps
`user`, `subtype` and `bot_id`, and the pipeline sets the flag once. `app_id` and
`bot_profile` are not kept by normalization; app posts carry `bot_id`.

```yaml
user_directory:
  enabled: true      # default true; false keeps rules (a)(b)(c) only
  ttl_seconds: 86400 # positive integer
  failure_cooldown_seconds: 300 # positive integer; no new lookup for a failed user
```

The service builds the directory when enabled and a reconcile source exists. Heartbeat
counters, present with the directory: `directory_lookups` (users.info calls),
`directory_failures`, `app_posts_human` (new message events with `bot_id` or `app_id`
judged human) and `bot_posts` (new message events judged bot).
