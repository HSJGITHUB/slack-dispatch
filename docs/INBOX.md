# Lane inbox and notifications

Run `python -m slack_dispatch.inbox.cli --ledger dummy.db --inbox-root inbox COMMAND`.
The installed entry point is `slack-dispatch`. Options can appear before or after
COMMAND. `SLACK_DISPATCH_LEDGER` and `SLACK_DISPATCH_INBOX` supply path defaults.
The default inbox root is `inbox`; a ledger path is required without an injected
store. This CLI consumes the shared LedgerStore API.

## Commands

- `inbox support`: list id, age, when, channel, thread root id (or `-`), kind,
  edited, attention, author and state. `--include-done` includes completed work.
  `--json` returns the same rows as an array, plus the raw message `ts` and
  `author_id`. Repeated ids collapse into one row. `when` is the message time as
  `MM-DD HH:MM` in `display_timezone`; age is kept. With `--config PATH` (or
  `SLACK_DISPATCH_CONFIG`) the optional top-level config keys `people`
  (user id -> display name) and `display_timezone` (IANA name, default `UTC`) apply:
  author shows the name when known, else the id. Without a config, authors are ids
  and times are UTC. An unreadable config returns 2. A zone the host cannot load
  (for example Windows without the tzdata package) prints one warning to stderr and
  falls back to UTC. When the CLI writes to the console it reconfigures stdout and
  stderr to UTF-8 with `errors="replace"`, so non-ASCII text survives a legacy code
  page such as cp949; streams injected by a caller are left unchanged.
- `ack slack:CDUMMY:1`: mark the message acknowledged.
- `done slack:CDUMMY:1 --note complete`: complete the message with an optional note.
- `reroute slack:CDUMMY:1 --to content`: route with reason `reroute:<old lane>`
  and append a `reroute` record to the destination inbox.
- `claim support --session dummy-session [--config PATH]`: replace the lane's ledger
  session pointer. Without `--session`, use `SLACK_DISPATCH_SESSION`, then
  `CLAUDE_SESSION_ID`. The value is stored verbatim and may be any form described in
  "Session values and precedence". With `--config` (or `SLACK_DISPATCH_CONFIG`) the
  CLI reads the lane's configured session; when it is non-null the claim is still
  stored but prints `warning: lane <lane> has a configured session; config wins ...`
  to stderr. An unreadable config returns 2 without writing. Without a config the
  CLI cannot tell and prints no warning.
- `queue`: show human-pending ids and classifier suggestions.
- `import-bindings PATH [--replace]`: import UTF-8 JSONL objects containing
  source, channel, thread_ts, and lane. Extra keys and blank lines are ignored.
  Validate all rows before writing. Keep existing bindings by default; --replace
  overwrites them. Count additions/replacements as added, existing rows as kept,
  and unknown lanes as skipped unknown lane. Print unknown lane ids without
  creating lanes. Duplicate keys in separate rows are processed in file order.
- `assign slack:CDUMMY:1 support --note reviewed`: record a human assignment with
  reason `human:cli`, optionally save a note, and append to the destination inbox.

A session first claims its stable lane, then reads its inbox. An operator reads
`queue` and explicitly uses `assign` to accept or override a suggestion. A suggestion
alone never changes ownership. Old inbox entries are hidden after rerouting.
Missing messages or destination lanes return 3, invalid input returns 2, and success returns 0.
Unexpected errors return 1 with one sanitized error line and no traceback.

Ordinary body output requires `--show-body` on `inbox` or `queue`.
Thread view (`inbox support --thread`) is the exception: bodies print by default.
Use `--no-body` to suppress bodies, including in JSON; it overrides `--show-body`.
Body requests use `get_message(id, include_body=True)`. Null bodies display
`[no body stored]`. Default listings, durable inbox records and notifications
never include bodies.
Header lines label tables; data lines begin with ids. Notes are not echoed.

## Durable formats and locking

Each `<root>/<lane>.jsonl` line has exactly `id`, `delivered_at`, `channel`, `ts`,
`thread_ts`, `followup`, and `kind`. Kinds include `new`, `followup`, `reroute`, and P7A `backfill`.
Since P9, kind `followup` is written only for an edited message (message_changed,
ledger `edited_at`). A reply delivered within the followup window of an earlier
delivery to its thread keeps its ordinary kind (`new` or `backfill`) with
`followup: true`.
The CLI also accepts unknown kind strings unchanged on read.
Records are append-only and may repeat ids. Read joins ledger state when a store
is supplied, hides completed work by default, and hides obsolete lane ownership.
Without a store, ledger state is unknown and completion cannot be inferred.
The age column measures seconds since the earliest delivery, with an `s` suffix
in text and JSON. Rows sort by that first delivery. Kind is the latest appended
non-followup kind (default `new` if all records are followups). Edited is `yes`
if any record has kind `followup` or the ledger has edited_at; the `followup` field
alone does not mark an edit. Otherwise it is `no`.
Missing attention and author values display `-`.

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
logs placeholder names only, never rendered text, arguments, or target values.
Templates and session pointers must be credential-free. The sample uses dummy
arguments and dry-run; building adapters does not execute commands.

## Session values and precedence

`lanes[].session` and claimed pointers accept three forms:

- a session name such as `dummy-session`, passed through as the `{target}`;
- a full session id (uuid), such as `0000aaaa-0000-4000-8000-000000000001`;
- a session id prefix of at least 8 hex characters, such as `0000aaaa`.

A value of 8 or more hex characters followed only by hex characters or hyphens is
an id form; anything else is a name. Id forms are resolved against the live session
files in `sessions_dir` (default `~/.claude/sessions`), one JSON file per session
holding at least `sessionId` and `name`. The matching file's `name` becomes the
`{target}`. An exact id match wins over prefix matches. No match skips the wake and
logs `session not live: lane <lane>` at INFO; more than one matching session id
skips the wake and logs `ambiguous session id` at WARNING. These lines are logged per
lane on state transitions only, with at most one `(reminder)` line per lane every 30
minutes while it stays undeliverable (see SERVICE.md). In both cases the item
stays in the file inbox, and the service delivery gate treats the wake as not
delivered: no delivery count, no timer receipt, and a later sweep retries it. When several
live sessions share the resolved name, `duplicate session name` is logged as a
warning: a name lane still sends (previous behaviour), an id lane uses the name of
the file whose `sessionId` matches. Directory scans are cached for 30 seconds per
process. Unreadable or incomplete session files are ignored. Logs carry the lane id
and counts only, never session values or names.

Precedence when building session targets (the service delivery gate uses the same rule):

| Setting | Config value | Effect |
| --- | --- | --- |
| session | non-null | Config wins; the ledger pointer (claim) is ignored |
| session | null | Ledger pointer applies (claim is the runtime override) |
| paused | true | Always paused, whatever the ledger says |
| paused | false | Ledger value applies when set |

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
when no new events arrive. `wake_policy.state_dir` defaults to `state`. The adapter
builder and the delivery gate read the switch through `wake_switch_path(config)`:
wake_policy.state_dir when set to a non-default value, else service.state_dir (see
SERVICE.md); the service passes the same directory to the engine. The global switch is
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

## Human queue digests

human_queue defaults to mode=digest, digest_seconds=1800 and immediate_flags
[always_human, owner_mention]. Ordinary items and overdue human timer notices
share one durable batch per adapter. The first pending item starts the interval;
no empty digest is sent. Counts include every unique pending message per channel,
while ids list at most 20 examples. Summaries include queue and assign commands,
followed by a last line `Lane ids for assign: <lane ids from config>`.
Immediate attention notices contain one id and report the assigned lane if any.
mode=immediate restores per-item notices. Human adapters bypass the global wake
switch; their dry_run still applies. A human_queue session_message adapter targets
`template.target` (default `operator`) with no lane lookup, claim, session
resolution or pause check.

Built-in session_message, file_drop and assistant_dm adapters carry summaries.
A human-purpose file_inbox uses atomic JSON notices in
<inbox-root>/human_queue-notices/ for summaries instead of lane inbox records.
A custom ids-only adapter receives ids through its existing notify method.
Retries and restart preserve per-adapter receipts. Resolved work is omitted from
pending batches. Changes to adapter order require operator review because receipt
identity includes the adapter position.


## P7B inbox context

`inbox support --thread [--owner UOWNER] [--full] [--json]` groups the same
filtered inbox items by `slack:<channel>:<thread_ts>`. Non-thread items are their
own roots. Each group shows the root metadata and first 400 body characters,
then pending replies in numeric timestamp order with complete bodies. `--full`
removes only the root truncation. A pending root appears once, as the root, with
its kind, edited and attention metadata. Context roots and ordinary roots are
both supported; absent roots say `root not in ledger`.

The latest qualifying owner reply newer than the earliest pending item adds
`owner replied at <ts>`. A qualifying row has route_reason `owner_post` or the
specified owner author. This is informational and does not hide pending work.
Owner detection reads metadata across lanes and beyond the API's 100-row default.
Thread JSON is an array of `{root, items}` groups; `items` is an array excluding
the root. Root objects include `missing`; groups may include `owner_replied_at`.
The `body` fields exist only when bodies are printed. An empty view is `[]`.

`ack ID [ID ...]` and `done ID [ID ...] [--note TEXT]` operate on each unique id.
Missing ids are reported by id, the rest are applied, and the command returns 3.
Bulk storage failures also report the failed id and continue, returning 3.
Unexpected errors outside bulk processing retain exit code 1. `ack --thread ROOT_ID [--lane LANE]`
and `done --thread ROOT_ID [--lane LANE] [--note TEXT]` operate only on pending
inbox items of the selected lane, not every ledger row in the thread. The root's
lane is the default. Without `--lane`, when the root is missing, has no lane (a
context row), or its lane holds no pending items of the thread, the lane is inferred
from the lanes whose inbox files hold pending items of the thread (for example a
rerouted reply). Ambiguous lanes print
`ambiguous lane for thread ROOT: a, b (use --lane)` and return 2 without mutation.
A missing root can still select replies. An unknown thread returns 3; an existing
root with no pending inbox work prints `no pending items for thread ROOT in lane X`
(`in any lane` when no lane is known) to stderr and returns 0. Acked items remain
pending until done. Do not combine ids and `--thread`; that returns 2.

Session adapters accept `template.message` and `template.cli` strings. Supported
message placeholders are `{lane}`, `{count}`, `{ids}`, `{kinds}`, `{attention}`,
`{lanes}`, and `{cli}`. Ids and lane ids are comma separated. Kinds count ledger
origin `reconcile` as backfill and all other or absent origins as new, in new then
backfill order, omitting zero counts. Attention counts ids with a set flag.
Unset cli is empty; unknown placeholders remain literal. Substitution is a single
pass, preserving braces in inserted values. An unset message keeps the existing
summary. Each command template part remains one argv element and no shell runs.
The sample wake requests thread context and preserves the session's own records.
