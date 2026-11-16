# Operator console

The console is a local web page for the human queue. Deterministic items still go to
lane sessions; items the router could not place wait in `human_pending`, and the
operator picks the lane with one click while the message body, the thread context
and the classifier suggestion are on screen. Since P25 the queue also lists the open items
of the direct lane (`console.direct_lane`), so it holds everything only the operator can
handle (see "P25 operator queue"). It also shows the lane inboxes, the
service state and a ledger search.

It is a local operator tool: no login, bound to 127.0.0.1 by default.

## Install and run

```sh
pip install -e .[console]
python -m slack_dispatch.console --config dummy-config.yaml
python -m slack_dispatch.console --config dummy-config.yaml --host 127.0.0.1 --port 8811
```

The `console` extra installs fastapi, uvicorn and httpx (httpx is only needed by the
offline tests). `--host` and `--port` win over `console.host` and `console.port`,
which default to 127.0.0.1 and 8811. Open `http://127.0.0.1:8811/`.

The console opens the same ledger as the service (SQLite with a 15 second busy
timeout) and runs the same idempotent migration as the inbox CLI. It never creates
a ledger: when `ledger.path` does not exist it prints `Ledger not found` and exits.

| Code | Meaning |
| --- | --- |
| 0 | Server stopped normally |
| 1 | The ledger could not be opened |
| 2 | Invalid configuration or arguments, missing ledger, or the console extra is not installed |
| 3 | uvicorn could not start, for example because the port is already in use |

uvicorn runs without an access log and without its own logging configuration, so
request lines (which can carry search text and message ids) are never logged. The
console logs nothing about messages; an unexpected request failure logs only the
exception class name.

## Configuration

Every key is optional:

```yaml
console:
  host: 127.0.0.1
  port: 8811
  base_url: null          # for example http://127.0.0.1:8811
  page_size: 50           # 1 to 500
  lane_hint_people: true
  direct_lane: owner      # default owner-direct; when set it must be a configured lane
  ui_language: en         # en (default) or ko: the language of the page
  undo_seconds: 30        # P19 undo window, at least 10
  workspace_domain: null  # P20 Slack subdomain for message permalinks
  sessions_table: null    # P21 Markdown sessions table path (session titles), read only
  channels:               # channel id -> display name, console only
    C0000000001: dummy-support
```

- `base_url` is read by the service for the links in human-queue notices (below).
- `direct_lane` is the lane behind the Direct button. The default is `owner-direct`;
  the button is shown only when that lane is configured. An explicit value must name
  a configured lane or the configuration fails to load.
- `lane_hint_people` adds author lane hints to each triage item: the lanes of
  `person_channel_rules` for the author, then the lanes the author's most recent 100
  lane-owned messages went to (up to five hints). Hints never assign anything.
- `channels` maps channel ids to names for the console only. People names come from
  the top-level `people` map and times use `display_timezone`, as in the inbox CLI.
- `ui_language` (P14) picks the page language, `en` or `ko`; any other value fails to
  load. `GET /api/config` returns it. Every page string lives in one dictionary per
  language in `console.js` (`UI_TEXT.en`, `UI_TEXT.ko`, same keys; a test checks that).

The lane inbox directory is the path of the first `file_inbox` adapter with purpose
`lane` (the directory the service writes), else `inbox.root`.

## Views

All pages are one static HTML, JS and CSS bundle served by the console (no build
step, no external assets). The page polls the current view every 20 seconds.

### Triage (`/`)

`GET /api/queue?offset=0&limit=50` lists `human_pending` messages (P25: plus the open
items of the direct lane, see "P25 operator queue") newest first by message time, `page_size` per page, with: when (display time zone), channel (name
from `console.channels`, else id), author (name from `people`, else id), attention,
origin, age, edited, files, bot, the queue reason (`note`), the classifier
suggestion and reason when stored, the author lane hints, the full body, and for a
reply the thread root (first 300 characters, or `missing` when the root is not in
the ledger). Thread context is the last five other messages of the thread from the
ledger, oldest first, each with author, when, state and the first 300 characters;
messages newer than the item are marked `after`. Bodies exist only when
`ledger.store_body` is true; otherwise the page shows "No body stored".

`pending_ids` (P14) lists every `human_pending` id across all pages, newest first, so
the page can drop staged decisions whose item left the queue.

Each item has a button per configured lane under the group label Assign (the
`human_queue` pseudo id is never offered); the suggested lane is first and highlighted,
hinted lanes are outlined. Direct, Exclude and Done (with an optional note) follow.

Staged decisions (P14). A decision button does not write. It stages the decision on the
row: the button is marked, the row gets a left border and a chip naming the decision. A
second click on the same button unstages it; another button replaces it. A Done note
typed in the row travels with the staged Done. The sticky bar at the top of the view
shows `GO (N)` (N = staged rows on every page), `Cancel all`, a one-line legend and,
after a commit, the result summary. GO sends every staged decision in one
`POST /api/queue/commit` (in chunks of 200); undecided rows are untouched. Applied
rows disappear; conflict and not_found rows are unstaged and show the reason; error
rows stay staged so GO can retry them. Staged decisions live in the browser's
localStorage (key `slack-dispatch.console.staged.v1`, by message id), so they survive
the 20 second poll and a reload; ids that leave the queue are dropped. A poll never
rebuilds or reorders an existing row, so staged rows keep their place and marks.
Each row also has a secondary `Apply now` link that applies only that row's staged
decision at once through `POST /api/queue/actions` (the pre-P14 behaviour). The bulk
checkbox bar is gone; staging replaces it.

Keys (shown in the page footer): `g` focuses GO; `Enter` on a focused row stages the
suggested lane; `Esc` unstages the focused row.

`POST /api/queue/commit` takes `{"decisions": [{"id", "action": "assign" | "direct" |
"exclude" | "done", "lane", "note"}]}` with 1 to 200 decisions and applies them in
order through the same action functions and guard as `POST /api/queue/actions`. It
returns `{"results": [...], "counts": {"applied", "conflict", "not_found", "error"}}`
with one result per decision in request order: `id`, `action`, `lane` (the destination
for assign and direct), `status` and `detail`. `conflict` means the item is no longer in
`human_pending` (someone handled it meanwhile; `detail` names the state). Per-decision
problems (missing or unknown lane, lane not in the ledger, direct lane not configured, a
repeated id) are `error` rows and never stop the batch.

Exclude follow-through (P14). An applied exclude result carries `channel_hint`: true
when the item's channel has no lane default (no config `channel_defaults` entry or a
null one, and no confirmed ledger claim; DMs and config-excluded channels never get a
hint), plus `channel` (`{id, name}`) when true. `POST /api/queue/actions` adds the same
fields to its exclude results. The page then shows one line suggesting the Scope tab,
with a link to it. Routing does not change.

`POST /api/queue/actions` takes `{"action": "assign" | "direct" | "exclude" | "done",
"ids": [...], "lane": ..., "note": ...}` with 1 to 200 ids and returns one result
per unique id (`ok`, `error` of `not_found`, `lane_not_found`, `conflict`, `invalid`
or `failed`, and `detail`). An unknown lane or a missing direct lane returns 400; a
configured lane missing from the ledger lanes table returns 404. Triage actions apply
only to messages still in `human_pending`; anything else is reported as `conflict`
and left unchanged, so a stale page cannot reopen handled work.

### Lanes (`/lanes`)

`GET /api/lanes` returns one card per configured lane: the effective session value
(config wins over a claim, as in the service) and whether it is live (id forms are
resolved with the service's `SessionResolver`; for a session name the console
reports whether a live session file has that name, while the service passes names
through unresolved), paused, whether the lane is registered in the ledger, today's
counts in the display time zone (`new` = received today, `assigned`, `acked`,
`done` = the matching timestamp is today), open items (new, assigned or acked),
the oldest unacknowledged age, the last delivery time from `service_deliveries`
(inbox appends and session wakes alike; the table does not tell them apart), the
last reminder wake (the unacknowledged timer in `timer_events`), and the inbox file
size.

`GET /api/lanes/{lane}/inbox?full=false` is the lane inbox as thread groups,
produced by the CLI's own `inbox LANE --thread --json` code with the configured
owner (`always_human.owner_user`) for the owner-replied marker. The console adds
`state` and `lane` per item, `pending` and `channel_name` on the root and a full
date (`when_full`). `full=true` lifts the 400 character root truncation.

`POST /api/lanes/{lane}/actions` takes `{"action": "ack" | "done" | "reroute",
"ids": [...], "lane": ..., "note": ..., "thread": false}`. `thread` (P24, default
false, reroute only) also moves every open item of each id's thread in the old lane and
the thread binding, like the CLI `reroute --thread`; the console page does not send it yet. Lane actions apply only to messages the
lane still owns (or that have no lane) and that are not done or excluded.

### Service (`/service`)

`GET /api/service` is read-only: heartbeat fields (status, ok, pid, version, ts,
started_at, last_event_at, last_catchup_at, gaps, counters) plus `stale` (older than
three heartbeat intervals, at least 90 seconds) and its age; the last five
`Reconcile pass` summaries and the last 20 WARNING, ERROR or CRITICAL lines from
the last 1 MiB of `<service.log_dir>/service.log` (the current file only, newest
first); the last 20 `ingest_gaps` rows; whether `wake.off` is present
(`wake_switch_path`); `wake_policy.enabled`; the human queue mode and digest
interval; and every notification adapter with its purpose and `dry_run` flag. There
are no controls.

### Scope (`/proposals`, P13)

`GET /api/proposals` returns `min_evidence`, the open and suggested proposals (suggested
first, then by evidence) and the `learned` panel. Each proposal has kind (`channel`, `dm`
or `person`), key, name (`console.channels` for conversations, `people` for persons),
lane, evidence count, first and last seen, status, `conflict`, the last evidence source,
`imported`, the lanes involved for the key with their counts, the current default
(`{lane, source}` with source `ledger`, `scope:auto` or `config`) and `effective` (this
lane is the current ledger override, for example a `scope:auto` claim to confirm).

`POST /api/proposals/actions` takes `{"action": "confirm" | "reject" | "defer", "kind",
"key", "lane"}`:

- `confirm` writes the confirmed claim, which the router's ScopeClaim rule reads at once
  (no YAML edit; it survives restarts). Confirmed claims of other lanes for the key go back
  to `claimed` and their confirmed proposals become `superseded`. The lane must be
  configured (400) and in the ledger (404).
- `reject` sets `rejected` (it stays rejected on new evidence) and returns the lane's
  confirmed claim for the key, such as a `scope:auto` one, to `claimed`.
- `defer` hides the proposal until new evidence reopens it.

Unknown proposals return 404; proposals that are not open or suggested return 409. The
`Learned scope` panel lists active confirmed channel, dm and person claims (who confirmed
them, when, and whether the config already has the same default). `GET
/api/proposals/export` returns `{"yaml": ...}`: `channel_defaults` and `dm_defaults`
entries to merge into the instance config, with person claims as comments (there is no
YAML key for them). The YAML stays the canonical place; the ledger override is what makes
a decision effective now.

### Search

`GET /api/search?q=TEXT` (2 to 200 characters) finds messages whose id contains the
text, whose channel or author id equals it, or whose stored body contains it, newest
first, at most 200 rows, with state, lane, route reason, note, suggestion and the
first 200 body characters. It answers "where did that message go".

## What an action writes

The console writes only through `slack_dispatch.console.actions`, which performs the
same LedgerStore calls and inbox appends as the inbox CLI:

| Console | Ledger | Inbox record |
| --- | --- | --- |
| assign LANE [note] | `set_route(id, LANE, "human:console")`; with a note `set_state(id, "assigned", note)` | `new` in LANE |
| direct | assign to `console.direct_lane` | `new` in that lane |
| exclude | `set_state(id, "excluded", note="human:console")` (the router's exclusion form) | none |
| done [note] | `set_state(id, "done", note)`; without a note the note is `human:console` | none |
| ack | `set_state(id, "acked")` | none |
| reroute LANE | `set_route(id, LANE, "reroute:<old lane>")` | `reroute` in LANE |
| assign, direct, reroute (P13, P24) | also `bind_thread(thread of id, LANE)` under the P24 rule (reroute: a thread root, or `thread: true`; assign and direct: a root, or a reply whose thread has no binding) and one `scope_proposals` event (a reroute to another lane, or an assign of a `human_pending` item) | as above |

The CLI records `human:cli` where the console records `human:console`; `done`
without a note is the one place the console writes a note where the CLI does not.
Since P13 `assign`, `direct` and `reroute` (console, CLI and card decisions alike) set the
thread binding of the message to the new lane and record scope learning evidence (see
the Scope view). Since P24 a reroute of a reply moves that reply only and leaves the
binding; a reroute of a thread root, or any reroute with `thread: true`, moves the
binding (with `thread: true` also the thread's open items in the old lane). `assign` and
`direct` of a reply bind only a thread without a binding (see INBOX.md, "Reroute and the
thread binding (P24)").

`assign`, `direct` and `reroute` also insert one row into the ledger table
`wake_requests` (P12; source `console`, `cli` for the CLI). No Slack call or session
command is made by the action itself. The resident service consumes pending wake
requests every 5 seconds and calls the wake engine `offer` once per (lane, message id)
still assigned to that lane, with the same debounce and storm rules as a socket
delivery, so the lane session is woken within about five seconds. The record is also
in the lane's durable inbox, where the session finds it on its next `inbox` read; the
service's timer sweep still sends the lane reminder after
`timers.unacknowledged_seconds` and a reassignment proposal after
`timers.reassign_seconds` while the item stays unacknowledged, exactly as for a CLI
assignment. The human queue digest drops handled items on its next flush, and
`state/human_queue.jsonl` is rewritten on the next human-pending event.

## Notice links

When `console.base_url` is set the service changes the human-queue notice text only:

- digests become `Human queue digest: N item(s); channels: C1=2, C2=1` (plus any
  assignment FYI and timer counts) and the line `Open: <base_url>/`; the id list,
  the CLI commands and the lane id line are omitted (P25 fix1: with a direct lane the
  digest ends with `Operator queue: undecided=N, direct=M (lane <direct lane>)`);
- immediate notices keep their text and end with `Open: <base_url>/#msg=<id>`, which
  opens the triage view and highlights that item (or searches for it when it has
  left the queue).

Adapter payload ids and metadata are unchanged. Without `base_url` the text is
exactly as before.

## Security model

- Loopback by default and no login. Binding another address needs `--host` or
  `console.host`; anyone who can reach that address can act on the queue.
- Every response sets `Cache-Control: no-store`.
- Requests whose Host is not 127.0.0.1, localhost, ::1, the bind host or the
  `base_url` host are refused (DNS rebinding), and so are non-GET requests whose
  Origin names another host. Action bodies must be JSON, which a cross-site form
  cannot send.
- Message bodies are untrusted data: the page renders them as text only, never as
  HTML.

## Windows task

`deploy/windows/install_console_task.ps1` renders
`deploy/windows/task_template_console.xml` exactly like the service installer:
UTF-8 template on disk, rendered UTF-16 LE XML with BOM, LogonTrigger plus a
TimeTrigger repeating every minute, `IgnoreNew`, no execution time limit, and only a
printed `schtasks.exe /Create` command (registration stays manual).

```powershell
deploy\windows\install_console_task.ps1 -Python C:\demo\python\pythonw.exe -Workdir C:\demo\release -Config C:\demo\state\config.yaml -User DEMO\operator
```

The task runs `pythonw.exe -X utf8 -B -m slack_dispatch.console --config <config>`.
The default task name is `SlackDispatchConsole`. A second instance cannot bind the
port and exits, so the minute trigger only restarts a stopped console.

## P15 channel exclusion and membership badge

Triage rows have an `Exclude channel` button (a Korean label in `ko`), staged like the other
decisions (action `exclude_channel` in `POST /api/queue/commit` and `POST
/api/queue/actions`, same `human_pending` guard). Applying it:

- writes a ledger row `channel_exclusions(id, channel, excluded_at, by, removed_at)` with
  `by` = `console` (one active row per channel), which the router honours at once like
  `excluded_channels` (see ROUTING.md);
- moves every open item of the channel (`new`, `assigned`, `acked`, `human_pending`; the
  clicked one included) to `excluded` with note `human:console:channel`.

The result carries `closed` (items moved) and `channel` (`{id, name}`); the page shows one
line naming the channel with a link to the Scope tab. A second staged row of the same
channel then reports `conflict` (its item was already closed).

A row shows the `not a member` badge (Korean label in `ko`) when the ledger table
`channel_membership` holds member channels and the item's channel is not one of them
(DMs and group DMs never get it). With an empty table no badge is shown.

The Scope tab adds an `Excluded channels (ledger)` card under Learned scope: channel,
name, by, when, whether the config already excludes it, and an `Undo` button (`POST
/api/channel-exclusions/undo` with `{"channel"}`; 404 without an active exclusion). Undo
ends the override only: items it closed stay `excluded`. The export button (`GET
/api/channel-exclusions/export`, `{"yaml"}`) returns an `excluded_channels` list: the
config entries plus the ledger ones (marked `# ledger`), to replace the config key. The
exclude follow-through hint is not shown for a channel the ledger already excludes.

## P17 owner lookup and registry sync

Available when `owner_resolver.enabled` (`GET /api/config` returns `owner_resolver:
true`).

- Registry badge. Queue items (`GET /api/queue`) and lane inbox groups and items
  (`GET /api/lanes/{lane}/inbox`) carry `registry` when a registry row matches the
  message's (channel, thread root or ts): `{id, title, status, owner, waiting_on,
  next_action, closed, last_ts, last_ts_newer, registry_closed, note}`, else null. The row
  shows a `registry <status>` badge (UI key `registryBadge`, both languages); hovering
  shows title, owner and next action.
- Registry closed. An item is `registry_closed` when its row is closed and `last_ts` is
  at or after the item's ts. Such a row gets `Done per registry (stage)` (UI key
  `registryDone`), which stages Done with note `registry:<status>:<registry id>`.
  `GET /api/queue` returns `registry_closed: [{id, note}]` for every pending item on all
  pages, and the queue toolbar shows `Stage all registry-closed (N)` (UI key
  `registryDoneAll`), which stages them all. Nothing is written until GO (P14). When
  `last_ts` is missing or older than the item, only the badge shows: the registry may be
  stale relative to the new post.
- Find owner (UI key `findOwner`). Queue rows and lane rows call `GET
  /api/owner?channel=...&ts=...` and show the candidates (tier, lane, session, source,
  evidence) under the row. On a queue row a candidate lane button stages the assign
  decision for that row. On a lane row it sets the Reroute target; Reroute applies it.
  The Scope tab has an `Owner lookup` card that takes a channel id and an optional ts.
- `GET /api/owner?channel=C...&ts=TS` (ts optional) returns `{channel, ts, thread_ts, root,
  best, candidates, registry_row, lanes}`. A ledger message with that ts supplies its
  thread root. `best` is `{lane, confidence, source, evidence}` or null; each candidate is
  `{lane, session, confidence, source, evidence}`. Invalid channel or ts: 400; resolver
  disabled: 404. It is read-only.

## P18 Deliveries tab

`/deliveries` (tab label `Deliveries`, `전달` in `ko`) answers "what did the dispatcher send
where, and did the session pick it up". It reads the ledger table `delivery_log` (see
SERVICE.md, "P18 delivery log"); it has no controls.

- Newest first, default window 48 hours, `console.page_size` rows per page. Filters: lane
  (the configured lanes and `human_queue`), purpose (`lane`, `human_queue`), status (`sent`,
  `failed`, `skipped` for every skipped reason, `dry_run`, `timeout`) and window (24h, 48h,
  7d, all).
- A summary line on top counts sent, failed (timeouts included), skipped and unconfirmed
  attempts in the window.
- Columns: time (display time zone; the full date on hover), kind, lane, target session
  (name and the first 8 hex characters of the session id, the adapter kind for card and
  file drop rows, else `no session`), items (expandable: id, channel name, author name via
  `people`, a thread badge for replies, `not in ledger` for unknown ids; never a body), the
  result badge (status plus the relay outcome, and `xN (last HH:MM)` when a skipped
  attempt repeated N times; the stored detail on hover), the receipt
  (`✓ HH:MM (+Nm)` when the lane acked or closed an item after the wake, `unconfirmed Nm`
  for a sent wake without one, `decision HH:MM` for a card that came back, `-` otherwise)
  and the relay cost and duration.

`GET /api/deliveries?lane=&purpose=&status=&hours=48&page=1` returns `{"hours", "page",
"page_size", "total", "rows"}`. `hours=0` means every row. Each row has `id`, `created_at`,
`when`, `time`, `purpose`, `kind`, `lane`, `adapter`, `target` (`{session_id, short_id,
name}` or null), `item_count`, `items` (`{id, missing, channel: {id, name}, is_reply,
author: {id, name}, state, lane}`), `status`, `relay_outcome` (`delivered`, `not_delivered`,
`unknown` or null), `detail`, `cost_usd`, `duration_ms`, `relay_session_id`,
`repeat_count` (1 when the attempt did not repeat), `last_seen_at`, `last_seen_time` and
`receipt`
(`received_at`, `received_time`, `latency_seconds`, `age_seconds`, `decision_at`,
`decision_time`). An unknown lane, purpose or status returns 400.

`GET /api/deliveries/summary?hours=48` returns `{"hours", "total", "sent", "failed",
"timeout", "skipped", "dry_run", "not_delivered", "unconfirmed"}`: `failed` includes
timeouts, `not_delivered` counts sent rows whose relay outcome is `not_delivered`, and
`unconfirmed` counts sent lane rows without a receipt.

The inbox CLI prints the same rows for an operator session:

```sh
python -m slack_dispatch.inbox.cli deliveries --ledger dummy/state/ledger.sqlite3 \
    [--config dummy-config.yaml] [--lane L] [--hours N] [--status S] [--json]
```

One line per row, newest first: time, kind, lane, target (`name(8 hex)`, the adapter kind
or `no-session`), item count, status with the relay outcome (`sent/delivered`, plus
` xN` for a repeated skipped attempt) and the
receipt (`received HH:MM (+Nm)`, `unconfirmed Nm`, `decision HH:MM` or `-`). `--hours`
defaults to 48 (0 = all), `--status` takes the console filter values, `--json` prints the
console row objects. With `--config` (or `SLACK_DISPATCH_CONFIG`) channel names, people and
`display_timezone` are used; without it ids and UTC. An invalid status or a negative
window exits with 2.

## P19 console undo

Immediate writes can be taken back for a short while. `console.undo_seconds` (default 30,
minimum 10; a smaller value or a non-integer fails to load) is the window. `GET /api/config`
returns `undo_seconds`.

### Action journal

Every console write that changes message rows records one row in the ledger table
`console_actions(id, at, action, ids, before, after, by, undo_until, undone_at, result,
meta)`:

| Console write | `action` | Rows journaled |
| --- | --- | --- |
| Triage `Apply now` (`POST /api/queue/actions`) | `assign`, `direct`, `exclude`, `exclude_channel` or `done` | the ids that changed |
| GO (`POST /api/queue/commit`) | `commit` | one row for the whole request, every id that changed (registry Done decisions included) |
| Lane row (`POST /api/lanes/{lane}/actions`) | `lane_ack`, `lane_done` or `lane_reroute` | the ids that changed |
| `exclude_channel` (either path) | as above | every open item of the channel it closed, plus the clicked one |

`before` and `after` are JSON lists with one object per message id: `id`, `state`, `lane`,
`note`, `done_at`, `ack_at`, `assigned_at` and `route_reason`. `ids` is a JSON list. `by`
is `console`. `at` and `undo_until` (`at` plus `undo_seconds`) are local ISO times. `meta`
is a JSON object with the target `lane` and the ids of the `wake_requests` rows (source
`console`) and `channel_exclusions` rows (by `console`) that the write added. When the write
changed the thread binding of a message (assign, direct, reroute), its `before` and `after`
entries also carry `binding`: `{source, channel, thread_ts, lane}` (lane null when the
thread had no binding). A write that
changed nothing (every id a conflict or not found) records no row. Scope proposal
decisions, claims and the Scope tab's channel exclusion `Undo` are not journaled; they
have their own controls. A GO of more than 200 decisions is sent in chunks, one journal
row per chunk.

The write responses carry `undo`: `{action_id, action, lane, count, at, undo_until,
seconds_left}`, or null when nothing was journaled, and `journal`: `ok` or `failed`.
Journaling never changes the write itself (fix1): when the journal cannot be read or
written, the action is still applied and answered as usual, `undo` is null, `journal` is
`failed`, and one WARNING `Console journal failed action=<action> class=<exception class>`
is logged.

### Undo

`POST /api/undo/{action_id}` with the body `{}` undoes one row while the console clock is
at or before `undo_until` and the row is not undone yet (404 unknown id, 409 `Already
undone` or `Undo window expired`). In one ledger transaction, for each id:

- `restored`: the row's `state` and `lane` still equal the `after` snapshot, so the seven
  `before` fields are written back exactly. The action's unconsumed `wake_requests` rows
  for the id are removed; when one was already consumed the id gets `wake_sent: true` (the
  lane saw a wake; it finds no pending item, nothing else to do).
- `conflict`: the state or lane changed meanwhile (`detail` names the current state and
  lane); the row and its wake requests are left unchanged.
- `not_found`: the message row is gone.

Thread bindings (fix1): a binding the action changed goes back to its earlier lane, or is
removed when the thread had none, when at least one restored message carries it and the
binding still has the action's lane. When it changed meanwhile it is left alone and those
ids get `binding: binding_conflict`. Restored ids otherwise get `binding: restored`, or
null when the action did not change a binding.

The `channel_exclusions` rows the action wrote are ended (`removed_at`) when still active;
an exclusion that existed before the action is never touched. When no message was restored
(every id a conflict or not found), `nothing_restored` is true and the exclusions are kept.
The response (also stored in `result`, with `undone_at`) is `{action_id, action, undone_at,
by, nothing_restored, results: [{id, status, detail, wake_sent, wake_removed, binding}],
counts: {restored, conflict, not_found, wake_sent, wake_removed, channel_exclusions_ended,
binding_restored, binding_conflict}}`.

Limits: undo does not remove the scope learning evidence (`scope_proposals`) that an assign
or reroute recorded, so an undone mistake still counts toward a scope suggestion; reject or
defer that proposal in the Scope tab if needed. The lane inbox record of an assign or
reroute stays in the file but is hidden: after a reroute is undone the ledger lane is the
old lane, and after an assign of a triage item is undone the row has no lane and is
`human_pending`, which the inbox CLI no longer lists (fix1).

`POST /api/undo/last` (body `{}`) undoes the most recent row that is not undone and still
inside its window (404 `Nothing to undo`). `GET /api/actions?limit=20` (1 to 200) returns
`{undo_seconds, actions}`, newest first, each with `id`, `at`, `when`, `time`, `action`,
`lane`, `count`, `ids`, `by`, `undo_until`, `undone_at`, `undoable`, `seconds_left` and the
stored `result`.

### Page

- After `Apply now`, a GO commit or a lane action, a bottom toast shows `<what happened> ·
  Undo (Ns)` (`되돌리기 (N초)` in `ko`) with a live countdown. Clicking it, or the `z` key
  while the toast shows, undoes that action and the toast shows the result (`Restored N`,
  plus `conflict N`, `not found N`, `wake already sent N` and kept thread bindings when
  not zero; `복구 N건`, `충돌 N건`, `깨우기는 이미 나감 N건` in `ko`). A write whose
  journal failed shows no toast. The toast disappears when the window ends.
- A lane row that was done or rerouted stays in place with `Handled · Undo (Ns)` (`처리됨`)
  for the window; polling skips the lane inbox meanwhile and reloads it when the window
  ends or after an undo. `Done` asks nothing extra.
- The Service tab starts with `Recent actions` (`최근 작업`): the last 20 journal rows with
  time, action, item count, by and an undo button while undoable, `expired` (`만료`)
  afterwards or `undone` (`되돌림`).

### CLI

The inbox CLI has `undo [--last | ACTION_ID] [--json]` (fix1), which calls the same code:

```sh
python -m slack_dispatch.inbox.cli --ledger dummy/state/ledger.sqlite3 undo --last [--json]
python -m slack_dispatch.console.actions --ledger dummy/state/ledger.sqlite3 undo --last [--json]
python -m slack_dispatch.console.actions --config dummy-config.yaml undo 12 [--json]
```

The ledger comes from `--ledger`, `SLACK_DISPATCH_LEDGER`, or `ledger.path` of `--config`
(or `SLACK_DISPATCH_CONFIG`); both options are also accepted after `undo`. Same rules as
the console; the result's `by` is `cli`. Text output is one summary line plus one line per
id that was not restored or whose wake was already sent; `--json` prints the result. Exit
codes: 0 undone (conflicts are listed), 1 already undone or the window expired, 2 invalid
arguments, configuration or missing ledger, 3 unknown action or nothing to undo.

## P20 display names and permalinks

### Names

Every user and channel the console shows goes through one resolver,
`queries.DisplayNames(config, store)`:

| Kind | Order |
| --- | --- |
| User | config `people`, then `user_directory.name` in the ledger, then the id |
| Channel | `console.channels`, then `channel_membership` in the ledger, then the id |

A membership row names a channel `#<name>` (public and private channels), a direct message
`DM:<partner name>` (the partner through the user order) and a group DM `group DM` (`그룹 DM`
in `ko`; UI key `groupDm`). Each request binds the view to the ledger (`View.bind(store)`), so
each table is read once per page build, never per row. A failed read shows ids (one WARNING
with the table and the exception class).

Names appear on queue rows (author, channel, thread root, thread context), lane thread views,
Deliveries item lists, search results, the Scope tab name column and exclude results. The id
stays in the element's `title`, so hovering shows it and it can still be copied. Mention lists
inside bodies and the registry badge owner text are left as they are, and owner candidates
keep session ids.

Payloads keep the `{id, name}` objects (name null when only the id is known) and add
`author_name` (queue items, their root and thread context, search rows), `channel_name`
(queue items, search rows, lane inbox roots and items) and `permalink` (below).

The service fills `user_directory` for every author (`user_directory.resolve_names`, see
INBOX.md "P20 names and links"), and `directory refresh` backfills older authors.

### Permalinks

```yaml
console:
  workspace_domain: dummy-workspace   # the Slack subdomain; null (default) hides the links
```

The value must be a Slack subdomain (`[a-z0-9][a-z0-9-]{0,62}`), like the dispatch card
adapter's `workspace_domain`. A message links to
`https://<domain>.slack.com/archives/<CHANNEL>/p<ts without the dot>`; a thread reply adds
`?thread_ts=<root ts>&cid=<CHANNEL>`. `queries.message_permalink(domain, channel, ts,
thread_ts=None)` builds it on top of the dispatch card's `permalink` helper; the card's own
output is unchanged.

`GET /api/config` adds `permalinks` (true when the domain is set). Payloads add `permalink`
on queue items, their root and context rows, search rows, Deliveries items, and lane inbox
roots and items (lane inbox keys only when the domain is set); Recent actions rows add
`links` (`{id, permalink}` for the first five ids). Without the domain every `permalink` is
null and the page shows no icon.

The page shows a small `↗` link next to each message (queue row, thread root line, thread
context, lane root and items, Deliveries items, search ids, Recent actions) that opens Slack
in a new tab (`target=_blank`, `rel=noopener noreferrer`; UI key `openInSlack`).

### Deliveries empty state

The delivery log started when P18 was deployed. When the filtered list is empty the tab shows
`Log started: <time>` (`기록 시작: <time>`) and, when the older per-thread record
`service_deliveries` has wakes in the same window before that start, `N earlier record(s) in
the old format (no details)` (`이전 방식 기록 N건 (상세 없음)`). `GET /api/deliveries` adds
`log_started_at` (UTC ISO), `log_started_when` (display time zone) and `legacy_deliveries`.

The start is stored once by the ledger migration in the `cursors` table (source `meta`, key
`delivery_log_started_at`): the first `delivery_log` row's time when rows already exist, else
the migration time. Readers take the earlier of that value and the first row.
`legacy_deliveries` is counted only for an empty window, honours the lane filter, and is 0 for
the human_queue purpose or lane and for status filters other than `sent` (the old record holds
lane wakes that were sent, one row per thread and lane with its latest time).

## P21 lane sessions and wake now

### Session identity

The operator knows a session by the title the IDE session list shows, not by its id. An
optional Markdown table supplies that title:

```yaml
console:
  sessions_table: dummy/state/SESSIONS.md   # null (default): no titles
```

The file is only read. Rows of the form

```text
| <last used> | <state> | <title> | <size> | `claude --resume <uuid>` |
```

are indexed by session id (lower case); a lookup takes the full id or a prefix that matches
exactly one row. Header, separator and malformed rows (fewer than five cells, a last cell
that is not a resume command with an id of at least 8 hex characters) are skipped; the
first row of an id wins; `\|` inside a title is kept as `|`. The file is parsed again only
when its modification time or size changes. A missing, unreadable or oversized (over 4 MiB)
file reads as an empty table.

`SessionResolver(sessions_dir, sessions_table=path)` (`notify.session_resolve`) adds
`title(value)` and `describe(value, paused=False)`, which returns `{id8, id, value,
addressed_by, name, live, title, state, table_state}` from the live session files and the
table, without logging and without touching the per-lane state the service uses. `name` is
the live session name (null when not live), `title` the table title (null without a row),
`state` one of `no_session`, `paused`, `live`, `not_live` or `ambiguous` (an id prefix that
matches several live sessions), `table_state` the table's state cell and `id8` the first 8
characters of the session id when known (a name-addressed session gets its id from the one
live session with that name).

Payloads:

- `GET /api/lanes`: each card adds `identity` (the `describe` result for the effective
  session and pause) and `wake` (`{allowed, reason, detail, assigned, acked}`, below). The
  P10 `session` object is unchanged.
- `GET /api/lanes/{lane}/inbox`: adds `identity` and `wake` for the lane header.
- `GET /api/queue`: adds `lane_sessions` (`{lane: identity}`) for the lane button tooltips.
- With `sessions_table` set only: Deliveries targets add `title`, and `GET /api/owner`
  candidates add `session_title`. Without it both payloads keep their P18 and P17 shapes.

Page: the Lanes cards and the lane inbox header show `Session: <title> (<live name>, <id8>)`
(`세션: ...` in `ko`; the title is replaced by the live name or the configured value when the
table has no row) and a badge when the session cannot be woken: `no session` (`세션 없음`),
`paused` (`일시 정지`) or `session off` (`세션 꺼짐`, not live or ambiguous). Queue lane
buttons get a `title` tooltip with the same text. Deliveries rows show the title before the
session name, and owner candidates show it after the session.

### Wake now

`POST /api/lanes/{lane}/wake` with `{}` or `{"ids": [...]}` (1 to 200 ids) re-delivers lane
items and asks the service to wake the lane session for them now:

- Without `ids`: the newest 20 items (by message ts) the lane owns in state `assigned` or
  `acked`, except deleted messages, context rows and owner posts (fix1). `capped` is true
  when more were eligible and `total` says how many; pressing again wakes the next ones,
  since items whose wake request is not consumed yet are skipped and counted in
  `already_queued`. With `ids`: each id must be such an item of this lane, else its result
  is `not_found`, `conflict` (`owned by lane X`, `state is X`, `message deleted`) or
  `not_inbox_item`; an id that still has an unconsumed wake request is reported `ok` with
  detail `already_queued` and not queued again; the other ids still go through.
- Each woken item gets one `wake_requests` row (source `console`, kind `wake_now`) and a
  `backfill` record in the lane inbox, unless the inbox file already holds a `backfill`
  record for it (`records_written`, `records_skipped`); message rows do not change. The
  service turns the requests into wake offers within about five seconds (SERVICE.md).
- Refused with 409 and a reason when the lane is paused (`Lane is paused`), has no session
  value (`Lane has no session`), its session is not live (`Lane session is not live`) or an
  id prefix matches several live sessions, or wakes are switched off (`wake.off` or
  `wake_policy.enabled: false`), since the service would drop the request. An unknown lane
  is 400, a lane missing from the ledger 404.
- The response is `{lane, queued, assigned, acked, total, capped, already_queued,
  records_written, records_skipped, results, wake_requests, undo, journal}`. One journal row
  (P19) with action `lane_wake`, `by` = `console`, the woken ids, `before` snapshots taken
  before the wake (fix1), `after` snapshots and `meta.wake_requests`. Undo within
  `console.undo_seconds` removes the unconsumed wake requests (`wake_removed`) and reports
  `wake_sent` for consumed ones; it writes back the pre-wake snapshot of each id whose state
  and lane did not change and reports the others as `conflict`. The inbox records stay (the
  item is still the lane's).

Acked items (fix1): the ledger column `wake_requests.kind` (`assign` by default for every
P12 request, `wake_now` for Wake now and the CLI `wake`) lets the service offer a `wake_now`
request for an item in state `assigned` or `acked`; `assign` requests keep the P12
assigned-only rule. A `wake_now` id that waits in the wake engine (debounce or storm batch)
is kept in the pipeline's in-memory `wake_now_ids` set, so the flush still sends it while
acked; it leaves the set once that wake was sent or dropped. Other acked deliveries (retries,
earlier deferred wakes) are not revived.

Page: the lane card and the lane inbox header have `Wake now` (`지금 깨우기`); when refused
the button is disabled and its tooltip names the reason. Each lane row has `Wake this item`
(`이 항목 깨우기`). The toast shows `Wake requests: N` (`깨우기 요청 N건`) with the P19 undo,
and the status line adds the cap and the already waiting count when they apply;
Recent actions list the row as `Wake requested` (`깨우기 요청`). The `Ack` button still only
marks an item as read.

The inbox CLI `wake LANE [IDS...] [--json]` applies the same rules (INBOX.md).

### DM items in the queue

Routing is unchanged. A direct message that no rule places (no thread binding, confirmed
claim, tag, DM default or person rule; see ROUTING.md) reaches the human queue like any
other unrouted message, with the owner resolver or classifier suggestion when one is
enabled. Lanes propose DM scope with
`scope add <lane> --dm D...`; the proposal appears in the Scope tab, and confirming it there
writes the confirmed claim that routes later DMs of that conversation to the lane.

- Queue items carry `is_dm` (channel type `im`) and the row shows a `DM` badge, so the
  operator sees that the decision picks the lane for a person's direct messages.
- `GET /api/proposals` adds `partner` (`{id, name}`, the `channel_membership` partner of the
  DM, named through `people` and `user_directory`; null for channel and person keys) to
  proposals and learned rows. The Scope tab lists DM proposals under `DM scope proposals`
  (`DM 범위 제안`) and confirmed DM claims under `DM claims` (`DM 소관 확정`), each with a
  Partner column.

## P25 operator queue

The triage queue is everything the operator must handle. Messages from the operator's own
channels are routed to the direct lane (`console.direct_lane`), which normally has no
session, so before P25 they waited in the Lanes tab while the queue said 0. Both sets are
now one queue.

### Rows

`queries.queue_page` (and `GET /api/queue`) returns, newest first by message time, both:

| `queue_kind` | Rows |
| --- | --- |
| `undecided` | `human_pending` messages (no lane could be resolved) |
| `direct` | messages whose lane is the configured direct lane, in state `assigned` or `acked` (context rows and owner posts left out) |

Without a configured direct lane (`console.direct_lane` not a configured lane) the queue is
`human_pending` only, as before. Each item adds `queue_kind`, `lane` (null for undecided
rows) and `direct_session` (true for a direct row while a session is attached to the
direct lane). Search, the registry badge and `Done per registry`, owner lookup, author
hints, names and permalinks work for both kinds.

`GET /api/queue?kind=all|undecided|direct` (default `all`; anything else is 422) filters
the rows. The payload adds `kind`, `counts: {undecided, direct}`, `direct_lane` and
`direct_session`; `total` is the filtered total. `counts`, `pending_ids` (every queue id of
both kinds, so a staged row hidden by the filter is not dropped) and `registry_closed`
always cover the whole queue. `GET /api/queue/counts` returns `{undecided, direct, total}`;
the page polls it on every view for the tab label. `GET /api/config` adds `operator_name`:
the `people` name of `always_human.owner_user`, or null.

### Actions

The row verbs are the same for both kinds (`Assign`, `Direct`, `Done`, `Exclude`, plus
`Exclude channel`) with the P14 staging, GO and `Apply now`. `POST /api/queue/commit` and
`POST /api/queue/actions` accept both kinds through `actions.operator_guard(direct lane)`:
an item still in `human_pending`, or an `assigned` or `acked` item of the direct lane.
Anything else is `conflict` with detail `state is <state>` as before.

| Decision | Undecided row | Direct row |
| --- | --- | --- |
| assign LANE | P10 assign | reroute to LANE (`reroute:<direct lane>`, `reroute` record, wake request, thread binding by the P24 rule, scope evidence); assign to the direct lane itself is an error |
| direct | assign to the direct lane | error `invalid`, detail `already in the direct lane` (commit status `error`); the page hides the button |
| done, exclude, exclude_channel | as before | as before |

Every write is journaled (P19) and can be undone; undoing a reroute puts the row back in
the direct lane and so back in the queue.

### Page

- The tab label counts both kinds: `Triage (N)` (`큐 (N)` in `ko`, UI key `tabQueueCount`).
- The queue header shows the split: `Lane needed N · <operator> direct N` (`레인 결정 필요 N ·
  <operator> 직접 N`, UI key `queueSplit`), where `<operator>` is `operator_name` or
  `Operator` (`운영자`). A filter toggle (`All`, `Lane needed`, `<operator> direct`; `전체`,
  `레인 결정 필요`, `<operator> 직접`) shows one kind or both; default both. The choice is kept
  in localStorage (`slack-dispatch.console.queue-kind.v1`) for this browser only.
- A direct row has a direct badge and a left border; it shows no `Direct` button and its
  lane buttons leave out the direct lane. A direct row cannot be staged as direct again
  (`Already in the direct lane`, `이미 직접 레인에 있음`). With a session attached to the
  direct lane the row shows `session attached` (`세션 있음`); when that session is not live
  (fix1) it shows `session down` (`세션 꺼짐`, item `direct_session_down`, page
  `direct_session_live`) instead. A row whose kind changed
  elsewhere (for example sent to the direct lane from the CLI) is rebuilt on the next poll,
  and a staged `Direct` of a row that is now direct is dropped.
- Lanes tab: `GET /api/lanes` keeps every lane card and adds per card `direct` and `listed`,
  plus `direct_lane: {lane, session_attached, listed, queue_count}` (null without a direct
  lane). While no session value (config or ledger claim) is attached to the direct lane,
  its card is not listed and the tab shows one line instead, `Operator items are in the
  queue` (`<operator> 직접 글은 큐에 있습니다` in `ko`), linking to the queue filtered to direct
  rows. When a session is attached (a future assistant session) the card is listed again
  and the queue still includes its items with the `session attached` note. fix1: when the
  attached session is not live the card stays listed with a `session down` (`세션 꺼짐`)
  badge (card `session_down`, `direct_lane.session_down`), so a dead session stays visible.

Notices and cards: the heartbeat `human_pending` counter and the immediate human queue
notices are unchanged; digests end with the operator queue counts line (fix1). Dispatch cards stay for `human_pending` attention items only; an attention item
routed to the direct lane gets the usual human-queue notice on the other adapters (`FYI,
assigned to lane <direct lane>`) and waits in the queue as a direct row. The inbox CLI
`queue` command lists both kinds (INBOX.md).
