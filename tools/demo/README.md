# Console demo seed

`seed_demo.py` writes a small, fully synthetic slack-dispatch instance so the operator
console can be opened with realistic content and no Slack workspace. It is meant for a
first look at the console and for screenshots.

Everything is fake. Channels use `C0DEMO...` ids (named like `#support-inbox` or
`#ops-alerts`), people use `U0DEMO...` ids and role names ("Ops Lead", "Designer A",
"Customer Success"), direct messages use `D0DEMO...`, and message bodies are short generic
work chatter. There are no tokens, the config has no Slack source, every notification
adapter is a dry run, and nothing in the seed or the console talks to Slack or any other
network service.

## Commands

```sh
pip install -e .[console]
python tools/demo/seed_demo.py --out /tmp/slack-dispatch-demo
python -m slack_dispatch.console --config /tmp/slack-dispatch-demo/config.yaml
```

The seed prints the exact console command for the directory it wrote. The console binds
to 127.0.0.1 only; open `http://127.0.0.1:8811/` (or the port given with `--port`).

Options:

- `--out DIR` (required): the target directory. It must be outside this repository (and
  not a directory that contains it) and empty; the seed refuses otherwise and exits with
  code 2 before writing anything.
- `--force`: allow a non-empty directory. Files an earlier demo run wrote there
  (`config.yaml`, `SESSIONS.md`, `state/`, `inbox/`, `logs/`, `sessions/`) are moved to
  `previous-N/` first; nothing is deleted.
- `--seed N`: the scenario seed (default 2611). The same seed gives the same messages,
  states and delivery history; another seed shifts the timing. Timestamps are always
  relative to the moment the seed runs and cover the last 48 hours.
- `--port PORT`: the console port written to the config (default 8811).

Run the seed again (with a new empty directory or `--force`) to refresh the timestamps;
the demo heartbeat counts as fresh for three days.

## What gets written

- `config.yaml`: six lanes (`operator` as the direct lane, `support`, `billing`,
  `content`, `data`, `ops`), channel and DM defaults, a mention-only `#leadership`
  channel, explicit tags, a person rule, and the console section. All paths are absolute
  and point into the demo directory.
- `state/ledger.sqlite3`: about 45 messages in every state (assigned, acked, done,
  human_pending, excluded, including `excluded:no_mention`, `excluded:bot` and
  `excluded:not_member`), thread replies with bindings, `owner_mention` and `always_human`
  flags, an edited message, an owner reply, confirmed scope claims, open and suggested
  scope proposals, a channel exclusion, channel membership and user directory rows, console
  journal rows and the delivery log.
- `state/heartbeat.json`, `logs/service.log`: what the Service tab reads.
- `inbox/<lane>.jsonl`: the lane inbox records behind the Lanes tab.
- `sessions/*.json` and `SESSIONS.md`: live session files and the sessions table that give
  lane sessions their titles. The `data` session has no live file, so it reads as not live.

The ledger is written through the package's own code (router, console actions, inbox
functions, delivery recorder, heartbeat writer). When a future change makes the scenario
no longer fit, the seed stops with an error instead of writing a misleading demo.

## What each tab shows

- Queue (`/`): five undecided items (`human_pending`) and three open items of the direct
  lane, with bodies, thread roots, attention badges, a DM, an edited post and lane hints.
- Lanes (`/lanes`): one card per lane with session identity: `support` and `ops` live,
  `billing` addressed by name, `content` paused, `data` not live, and the direct lane
  linked to the queue. Each lane inbox lists its open threads; `support` shows an owner
  reply marker and an unacknowledged reminder.
- Service (`/service`): a fresh heartbeat with counters, recent reconcile passes, one
  warning and one error line, two ingest gaps, the dry-run adapters and the recent console
  actions.
- Scope (`/proposals`): one suggested and two open proposals, two learned (confirmed)
  claims and one ledger channel exclusion.
- Deliveries (`/deliveries`): lane wakes with receipts, unconfirmed wakes, a timeout and
  its retry, a not-delivered relay result, paused and no-session skips, a reminder, one
  digest, one dispatch card and attention notices.
- Search: try `export` or `billing`.

Console actions (assign, done, wake now, undo) work on the demo ledger and only change
files in the demo directory.
