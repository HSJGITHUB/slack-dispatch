# Operator console

The console is a local web page for the human queue. Deterministic items still go to
lane sessions; items the router could not place wait in `human_pending`, and the
operator picks the lane with one click while the message body, the thread context
and the classifier suggestion are on screen. It also shows the lane inboxes, the
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

`GET /api/queue?offset=0&limit=50` lists `human_pending` messages newest first by
message time, `page_size` per page, with: when (display time zone), channel (name
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
"ids": [...], "lane": ..., "note": ...}`. Lane actions apply only to messages the
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
| assign, direct, reroute (P13) | also `bind_thread(thread of id, LANE)` and one `scope_proposals` event (a reroute to another lane, or an assign of a `human_pending` item) | as above |

The CLI records `human:cli` where the console records `human:console`; `done`
without a note is the one place the console writes a note where the CLI does not.
Since P13 `assign`, `direct` and `reroute` (console, CLI and card decisions alike) set the
thread binding of the message to the new lane and record scope learning evidence (see
the Scope view).

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
  assignment FYI and timer counts) and a last line `Open: <base_url>/`; the id list,
  the CLI commands and the lane id line are omitted;
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
