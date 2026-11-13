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

Each item has a button per configured lane (the `human_queue` pseudo id is never
offered); the suggested lane is first and highlighted, hinted lanes are outlined.
Direct, Exclude and Done (with an optional note) follow. Checkboxes select items for
the same actions in bulk. After an action the row disappears.

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

The CLI records `human:cli` where the console records `human:console`; `done`
without a note is the one place the console writes a note where the CLI does not.
Like the CLI `assign`, a console assignment does not create a thread binding.

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
