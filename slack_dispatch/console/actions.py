"""Console write actions.

Each function performs the same LedgerStore calls and inbox append as the inbox CLI
command of the same name (`slack_dispatch/inbox/cli.py`), so the ledger and the lane
inbox files change exactly as they would from the CLI. Where the CLI records
`human:cli`, the console records `human:console`. `exclude` has no CLI command; it
uses the same call the router uses for exclusions (state `excluded`, reason in note).

Nothing here posts anywhere. Since P12 `assign` and `reroute` also queue one row in
`wake_requests` per delivered message; the service consumes it and offers the lane wake
(see docs/CONSOLE.md). The inbox CLI `assign` and `reroute` call these same functions.

Since P13 `assign` and `reroute` also point the message's thread binding at the new lane,
so later replies follow it, and record scope learning evidence (a `scope_proposals` row):
every reroute to another lane, and an assignment of an item that was `human_pending`.
Card decisions use `assign` and share both behaviours.

Since P24 the binding rule depends on the message (`is_thread_root`): `reroute` of a reply
moves that reply only and leaves the binding; `reroute` of a thread root moves the binding.
`reroute(..., thread=True)` (CLI `--thread`, `apply(..., thread=True)` for the console)
also moves every open item of the thread that is in the old lane and points the binding at
the new lane. `assign` of a root binds as before; `assign` of a reply binds only a thread
without a binding.

Since P15 `exclude_channel` (console only, no CLI command) writes a ledger channel
exclusion that the router reads at runtime like `excluded_channels`, and moves every open
item of the channel to `excluded` with note `human:console:channel`.

Since P25 the triage queue is the operator queue: `human_pending` items plus the open
items of the direct lane (`operator_guard`); `apply(..., direct_lane=...)` turns an assign of
a direct-lane item into a reroute (see `apply`).

Since P19 every console write that changes message rows is journaled in the ledger table
`console_actions` (see `Journal`) and can be undone for `console.undo_seconds` with `undo`
or `undo_last`, from the console or with
`python -m slack_dispatch.console.actions undo [--last | ACTION_ID] [--json]`.
"""
import argparse
from datetime import datetime
import json
import logging
import math
import os
import sys
import time

from slack_dispatch.inbox.files import append, record, validate_lane
from slack_dispatch.ledger.store import OPEN_STATES, SNAPSHOT_FIELDS, UndoStateError
from slack_dispatch.router.apply import record_scope_proposal

LOG = logging.getLogger(__name__)
REASON = "human:console"
CHANNEL_REASON = "human:console:channel"
MAX_IDS = 200

QUEUE_ACTIONS = ("assign", "direct", "exclude", "exclude_channel", "done")
LANE_ACTIONS = ("ack", "done", "reroute")
# P14 staged queue decisions: one commit status per decision.
COMMIT_STATUSES = ("applied", "conflict", "not_found", "error")


class ActionError(Exception):
    """A per-message failure with a stable code: not_found, lane_not_found, conflict or
    not_inbox_item (P12)."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


def inbox_refusal(message):
    """One-line reason when a ledger row is never an inbox item, else None.

    Context rows (route_reason `context:*` or note `context`) and owner posts
    (route_reason or note `owner_post`) are shown only as thread roots or owner-replied
    markers; assign, reroute, cutover and card decisions refuse them.
    """
    reason = str(message.get("route_reason") or "")
    note = str(message.get("note") or "")
    if reason.startswith("context:") or note == "context":
        return "refused: " + str(message.get("id")) + " is a context row, not an inbox item"
    if reason == "owner_post" or note == "owner_post":
        return "refused: " + str(message.get("id")) + " is an owner post, not an inbox item"
    return None


def request_wake(store, lane, message_id, source):
    """Queue one lane wake for a delivered message (P12). Stores without the table skip it."""
    queue = getattr(store, "request_wake", None)
    if callable(queue):
        queue(lane, message_id, source)


def _source(reason):
    return reason.split(":", 1)[1] if reason.startswith("human:") else reason


def _message(store, message_id):
    message = store.get_message(message_id)
    if message is None:
        raise ActionError("not_found")
    return message


def _lane(store, lane):
    validate_lane(lane)
    if store.get_lane(lane) is None:
        raise ActionError("lane_not_found")


def thread_key(message):
    """(source, channel, thread root ts) of the message's thread, or None (P24)."""
    root = message.get("thread_ts") or message.get("ts")
    if message.get("channel") and root:
        return (message.get("source") or "slack", message["channel"], root)
    return None


def is_thread_root(message):
    """P24: a message that is not a reply (no thread_ts, or thread_ts equal to its ts)."""
    return not message.get("thread_ts") or message.get("thread_ts") == message.get("ts")


def bind_message_thread(store, message, lane, *, only_unbound=False):
    """Point the message's thread (its thread_ts, else its own ts) at the lane (P13).

    P24: with `only_unbound` an existing binding is kept. Returns True when it bound."""
    key = thread_key(message)
    if key is None:
        return False
    if only_unbound and store.lookup_binding(*key) is not None:
        return False
    store.bind_thread(*key, lane)
    return True


def thread_items(store, message, lane):
    """P24: ids of the open items (OPEN_STATES) of the message's thread that sit in `lane`,
    thread root included, oldest first. Context rows and owner posts are left out. Stores
    without `thread_messages` (test fakes) return none."""
    key = thread_key(message)
    lookup = getattr(store, "thread_messages", None)
    if key is None or not lane or not callable(lookup):
        return []
    return [row["id"] for row in lookup(*key)
            if row.get("lane") == lane and row.get("state") in OPEN_STATES
            and not inbox_refusal(row)]


def assign(store, inbox_root, message_id, lane, *, note=None, reason=REASON, min_evidence=None):
    """CLI `assign ID LANE [--note]`: route, optional note, bind the thread to the lane,
    append a `new` inbox record, queue a lane wake (source = the reason without `human:`).
    Assigning a `human_pending` item records scope learning evidence (P13).
    P24: a root binds its thread as before; a reply binds only a thread without a binding."""
    message = _message(store, message_id)
    _lane(store, lane)
    refusal = inbox_refusal(dict(message, id=message_id))
    if refusal:
        raise ActionError("not_inbox_item", refusal)
    store.set_route(message_id, lane, reason)
    if note:
        store.set_state(message_id, "assigned", note=note)
    bind_message_thread(store, message, lane, only_unbound=not is_thread_root(message))
    append(inbox_root, lane, [record(dict(message, id=message_id), "new")])
    request_wake(store, lane, message_id, _source(reason))
    if message.get("state") == "human_pending":
        record_scope_proposal(store, message, lane, "assign", min_evidence=min_evidence)
    return message


def reroute(store, inbox_root, message_id, lane, *, source="console", min_evidence=None,
            thread=False):
    """CLI `reroute ID --to LANE [--thread]`: reason `reroute:<old lane>`, append a `reroute`
    record, queue a lane wake. A reroute to another lane records scope learning evidence
    (P13).

    P24: the thread binding moves to the new lane only for a thread root or with `thread`;
    a reply rerouted alone leaves it. With `thread` every other open item of the thread in
    the old lane (root included) is rerouted the same way (reason, record, wake request);
    the learning evidence stays one event per call."""
    message = _message(store, message_id)
    _lane(store, lane)
    refusal = inbox_refusal(dict(message, id=message_id))
    if refusal:
        raise ActionError("not_inbox_item", refusal)
    old = message.get("lane")
    others = ([mid for mid in thread_items(store, message, old) if mid != message_id]
              if thread and old and old != lane else [])
    for mid, row in [(message_id, message)] + [(mid, store.get_message(mid)) for mid in others]:
        if row is None:
            continue
        store.set_route(mid, lane, "reroute:" + str(row.get("lane") or "none"))
        append(inbox_root, lane, [record(dict(row, id=mid), "reroute")])
        request_wake(store, lane, mid, source)
    if thread or is_thread_root(message):
        bind_message_thread(store, message, lane)
    if old != lane:
        record_scope_proposal(store, message, lane, "reroute", min_evidence=min_evidence)
    return message


def ack(store, message_id):
    """CLI `ack ID`."""
    message = _message(store, message_id)
    store.set_state(message_id, "acked", note=None)
    return message


def done(store, message_id, *, note=None):
    """CLI `done ID [--note]`."""
    message = _message(store, message_id)
    store.set_state(message_id, "done", note=note)
    return message


def exclude(store, message_id, *, reason=REASON):
    """State `excluded` with the reason in the note, as the router records exclusions."""
    message = _message(store, message_id)
    store.set_state(message_id, "excluded", note=reason)
    return message


def exclude_channel(store, message_id, *, by="console"):
    """P15 `Exclude channel`: record the ledger channel exclusion of the message's channel
    and move every open item of that channel (this one included) to `excluded` with note
    `human:console:channel`. Returns the number of items closed."""
    message = _message(store, message_id)
    channel = message.get("channel")
    if not channel:
        raise ActionError("invalid", "message has no channel")
    store.add_channel_exclusion(channel, by)
    return store.close_channel_items(channel, CHANNEL_REASON, source=message.get("source") or "slack")


def queue_guard(message):
    """Triage actions apply only to items still waiting in the human queue."""
    if message["state"] != "human_pending":
        raise ActionError("conflict", "state is " + str(message["state"]))


DIRECT_STATES = ("assigned", "acked")
ALREADY_DIRECT = "already in the direct lane"


def is_direct_item(message, direct_lane):
    """P25: an open item of the direct lane (`assigned` or `acked`), a `direct` queue row."""
    return (bool(direct_lane) and message.get("lane") == direct_lane
            and message.get("state") in DIRECT_STATES)


def operator_guard(direct_lane):
    """P25 queue guard: items still in `human_pending`, or open items of the direct lane.

    Anything else (handled meanwhile, moved to another lane) is a conflict with the P10
    detail `state is <state>`, so a stale page cannot reopen handled work."""
    def check(message):
        if message["state"] == "human_pending" or is_direct_item(message, direct_lane):
            return
        raise ActionError("conflict", "state is " + str(message["state"]))
    return check


def lane_guard(lane):
    """Lane inbox actions apply only to open items the lane still owns."""
    def check(message):
        if message.get("lane") not in (None, lane):
            raise ActionError("conflict", "owned by lane " + str(message.get("lane")))
        if message["state"] in ("done", "excluded"):
            raise ActionError("conflict", "state is " + str(message["state"]))
    return check


def apply(store, inbox_root, action, ids, *, lane=None, note=None, guard=None, min_evidence=None,
          journal=None, thread=False, direct_lane=None):
    """Apply one action to each unique id in order; return one result per id.

    P25: with `direct_lane` (the operator queue) a `direct` queue row (an open item of the
    direct lane) takes `assign` as a reroute to the chosen lane (reason `reroute:<direct
    lane>`, `reroute` inbox record, wake request, journaled like any write), refuses an
    assign to the direct lane itself and `direct` with error `invalid` (detail `already in
    the direct lane`); `done`, `exclude` and `exclude_channel` work as for `human_pending`.

    P24: `thread` (reroute only) also moves the open items of each id's thread that are in
    its old lane and the thread binding (see `reroute`); the journal watches them too.

    `lane` is the destination for assign, direct and reroute. `note` is recorded by
    assign (when set) and done; done without a note records `human:console`. A guard
    may refuse an id with ActionError("conflict"). Failures never stop the batch.
    `min_evidence` is `scope_learning.min_evidence` for the learning events (P13).
    `journal` (P19) snapshots each id, and every open item of the channel for
    exclude_channel, before it changes; the caller records it with `Journal.finish`.
    """
    results = []
    for message_id in dict.fromkeys(ids):
        extra = {}
        try:
            if journal is not None:
                journal.watch([message_id])
            message = store.get_message(message_id)
            if message is None:
                raise ActionError("not_found")
            if guard is not None:
                guard(message)
            if journal is not None and action == "exclude_channel" and message.get("channel"):
                journal.watch(journal.channel_items(message))
            if journal is not None and action == "reroute" and thread:
                journal.watch(thread_items(store, message, message.get("lane")))
            if action in ("assign", "direct") and is_direct_item(message, direct_lane):
                if action == "direct" or lane == direct_lane:
                    raise ActionError("invalid", ALREADY_DIRECT)
                reroute(store, inbox_root, message_id, lane, min_evidence=min_evidence)
            elif action in ("assign", "direct"):
                assign(store, inbox_root, message_id, lane, note=note, min_evidence=min_evidence)
            elif action == "reroute":
                reroute(store, inbox_root, message_id, lane, min_evidence=min_evidence,
                        thread=thread)
            elif action == "exclude":
                exclude(store, message_id)
            elif action == "exclude_channel":
                extra["closed"] = exclude_channel(store, message_id)
            elif action == "done":
                done(store, message_id, note=note or REASON)
            elif action == "ack":
                ack(store, message_id)
            else:
                raise ValueError("Unsupported action")
        except ActionError as error:
            results.append(dict(id=message_id, ok=False, error=error.code, detail=error.detail))
            continue
        except ValueError:
            results.append(dict(id=message_id, ok=False, error="invalid", detail=None))
            continue
        except Exception:
            # Mirror the CLI: report the id, keep going, never echo exception text.
            results.append(dict(id=message_id, ok=False, error="failed", detail=None))
            continue
        results.append(dict(id=message_id, ok=True, error=None, detail=None, **extra))
    return results


def commit_status(result):
    """Map one `apply` result to a P14 commit status: applied, conflict, not_found or error."""
    if result["ok"]:
        return "applied"
    if result["error"] in ("conflict", "not_found"):
        return result["error"]
    return "error"


# P19 console undo: action journal, undo and the operator CLI.

JOURNAL_BY = "console"
UNDO_COUNTS = ("restored", "conflict", "not_found", "wake_sent", "wake_removed",
               "channel_exclusions_ended", "binding_restored", "binding_conflict")


def stamp_at(epoch):
    """Local ISO time with offset for a journal stamp (epoch seconds)."""
    return datetime.fromtimestamp(float(epoch)).astimezone().isoformat()


def seconds_left(row, now):
    """Whole seconds left in a journal row's undo window (0 when expired or undone)."""
    if row.get("undone_at"):
        return 0
    try:
        until = datetime.fromisoformat(row.get("undo_until") or "")
    except (TypeError, ValueError):
        return 0
    if until.tzinfo is None:
        until = until.astimezone()
    return max(0, math.ceil(until.timestamp() - float(now)))


def undoable(row, now):
    """True while a journal row is not undone and `now` is at or before `undo_until`."""
    if row.get("undone_at"):
        return False
    try:
        until = datetime.fromisoformat(row.get("undo_until") or "")
    except (TypeError, ValueError):
        return False
    if until.tzinfo is None:
        until = until.astimezone()
    return float(now) <= until.timestamp()


class Journal:
    """Collect one console write for the `console_actions` journal (P19).

    `watch(ids)` snapshots ids (state, lane, note, done_at, ack_at, assigned_at,
    route_reason, and the thread binding of the message) the first time they are seen,
    before anything changes them. `finish` snapshots them again and records one journal row
    with the ids that changed, the wake_requests rows (source `console`) and
    channel_exclusions rows (by `console`) the write added. An entry carries `binding`
    (`{source, channel, thread_ts, lane}`, lane null for no binding) when the write changed
    the binding of its thread (assign, direct, reroute). Stores without the journal methods
    (test fakes) record nothing.

    fix1: journaling never changes the write itself. Any journal error (in the constructor,
    `watch` or `finish`) turns journaling off for this write, logs one WARNING with the
    exception class only, and sets `failed`; `finish` then returns None.
    """

    def __init__(self, store, action, *, lane=None, by=JOURNAL_BY):
        self.store, self.action, self.lane, self.by = store, action, lane, by
        self.enabled = callable(getattr(store, "add_console_action", None))
        self.failed = False
        self.before = {}
        self.bindings = {}
        self.missing = set()
        if self.enabled:
            try:
                self.wake_mark = store.wake_request_mark()
                self.exclusion_mark = store.channel_exclusion_mark()
            except Exception as error:
                self._fail(error)

    def _fail(self, error):
        LOG.warning("Console journal failed action=%s class=%s", self.action, type(error).__name__)
        self.enabled = False
        self.failed = True

    @property
    def status(self):
        """`failed` when journaling broke for this write, else `ok` (response field)."""
        return "failed" if self.failed else "ok"

    def watch(self, ids):
        if not self.enabled:
            return
        try:
            fresh = [mid for mid in ids if mid not in self.before and mid not in self.missing]
            if not fresh:
                return
            found = self.store.message_snapshots(fresh)
            bindings = self.store.binding_snapshots(fresh)
            for mid in fresh:
                if mid in found:
                    self.before[mid] = found[mid]
                    self.bindings[mid] = bindings.get(mid)
                else:
                    self.missing.add(mid)
        except Exception as error:
            self._fail(error)

    def channel_items(self, message):
        """Open item ids of the message's channel (empty when journaling is off or fails)."""
        if not self.enabled:
            return []
        try:
            return self.store.open_channel_ids(message["channel"],
                                               source=message.get("source") or "slack")
        except Exception as error:
            self._fail(error)
            return []

    def finish(self, undo_seconds, now):
        """Record the journal row when a watched row changed; return the undo info or None."""
        if not self.enabled or not self.before:
            return None
        try:
            return self._record(undo_seconds, now)
        except Exception as error:
            self._fail(error)
            return None

    def _record(self, undo_seconds, now):
        after = self.store.message_snapshots(list(self.before))
        after_bindings = self.store.binding_snapshots(list(self.before))
        ids = [mid for mid in self.before
               if mid in after and _snapshot(after[mid]) != _snapshot(self.before[mid])]
        if not ids:
            return None
        before_rows, after_rows = [], []
        for mid in ids:
            old, new = dict(_snapshot(self.before[mid]), id=mid), dict(_snapshot(after[mid]), id=mid)
            old_binding, new_binding = self.bindings.get(mid), after_bindings.get(mid)
            if old_binding and new_binding and old_binding != new_binding:
                old["binding"], new["binding"] = old_binding, new_binding
            before_rows.append(old)
            after_rows.append(new)
        wakes = [row["id"] for row in self.store.wake_requests_after(self.wake_mark, ids, self.by)]
        exclusions = [row["id"] for row in
                      self.store.channel_exclusions_after(self.exclusion_mark, self.by)]
        at = stamp_at(now)
        until = stamp_at(float(now) + undo_seconds)
        action_id = self.store.add_console_action(
            at=at, action=self.action, ids=ids, before=before_rows, after=after_rows,
            by=self.by, undo_until=until,
            meta={"lane": self.lane, "wake_requests": wakes, "channel_exclusions": exclusions})
        return {"action_id": action_id, "action": self.action, "lane": self.lane,
                "count": len(ids), "at": at, "undo_until": until, "seconds_left": int(undo_seconds)}


def _snapshot(row):
    return {name: row.get(name) for name in SNAPSHOT_FIELDS}


def undo(store, action_id, *, now, by=JOURNAL_BY):
    """Undo one journal row (see `LedgerStore.undo_console_action`). Raises KeyError for an
    unknown id and UndoStateError (`undone`, `expired`) when it is no longer undoable."""
    return store.undo_console_action(action_id, at=stamp_at(now), now_epoch=now, by=by)


def last_undoable(store, now):
    """The most recent journal row not undone yet and still inside its window, or None."""
    for row in store.list_console_actions(50, pending_only=True):
        if undoable(row, now):
            return row
    return None


def undo_last(store, *, now, by=JOURNAL_BY):
    """Undo the most recent undoable action; KeyError when there is none."""
    row = last_undoable(store, now)
    if row is None:
        raise KeyError("last")
    return undo(store, row["id"], now=now, by=by)


def undo_text(result):
    """One summary line plus one line per id that was not restored (CLI output)."""
    counts = result["counts"]
    lines = ["undone action " + str(result["action_id"]) + " (" + str(result["action"]) + "): "
             + ", ".join(name + " " + str(counts.get(name, 0)) for name in UNDO_COUNTS)]
    if result.get("nothing_restored"):
        lines.append("  nothing_restored: channel exclusions and bindings left unchanged")
    for row in result["results"]:
        if row["status"] != "restored" or row["wake_sent"] or row.get("binding") == "binding_conflict":
            lines.append("  " + row["id"] + " " + row["status"]
                         + (" (" + row["detail"] + ")" if row["detail"] else "")
                         + (" wake_sent" if row["wake_sent"] else "")
                         + (" binding_conflict" if row.get("binding") == "binding_conflict" else ""))
    return "\n".join(lines)


def build_undo_parser():
    parser = argparse.ArgumentParser(prog="python -m slack_dispatch.console.actions",
                                     description="Undo a recent console action (P19)")
    parser.add_argument("--ledger", default=None, help="ledger path (else SLACK_DISPATCH_LEDGER "
                        "or ledger.path of --config / SLACK_DISPATCH_CONFIG)")
    parser.add_argument("--config", default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    sub = commands.add_parser("undo")
    target = sub.add_mutually_exclusive_group(required=True)
    target.add_argument("--last", action="store_true")
    target.add_argument("action_id", nargs="?", type=int)
    sub.add_argument("--json", action="store_true")
    # --ledger and --config are accepted before and after the subcommand.
    sub.add_argument("--ledger", default=argparse.SUPPRESS)
    sub.add_argument("--config", default=argparse.SUPPRESS)
    return parser


def _ledger_path(args):
    path = args.ledger or os.environ.get("SLACK_DISPATCH_LEDGER")
    if path:
        return path
    config_path = args.config or os.environ.get("SLACK_DISPATCH_CONFIG")
    if not config_path:
        return None
    from slack_dispatch.config.loader import load_config
    return load_config(config_path).ledger.path


def undo_main(argv=None, *, store=None, stdout=None, stderr=None, clock=time.time):
    """`undo [--last | ACTION_ID] [--json]` for an operator session.

    Exit codes: 0 undone (the output lists conflicts), 1 not undoable any more (already
    undone or the window expired), 2 invalid arguments, configuration or ledger, 3 no such
    action or nothing to undo.
    """
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        args = build_undo_parser().parse_args(argv)
    except SystemExit as error:
        return 2 if error.code else 0
    owned = False
    if store is None:
        try:
            path = _ledger_path(args)
        except ValueError:
            print("Invalid configuration", file=stderr)
            return 2
        if not path or not os.path.exists(path):
            print("Ledger not found", file=stderr)
            return 2
        from slack_dispatch.ledger.store import LedgerStore
        store = LedgerStore(path)
        store.open()
        owned = True
    try:
        store.migrate()
        return undo_command(store, last=args.last, action_id=args.action_id, as_json=args.json,
                            stdout=stdout, stderr=stderr, now=clock())
    finally:
        if owned:
            store.close()


def undo_command(store, *, last, action_id, as_json, stdout, stderr, now, by="cli"):
    """The shared body of `undo` (this module and the inbox CLI): undo, print, exit code."""
    try:
        if last:
            result = undo_last(store, now=now, by=by)
        else:
            result = undo(store, action_id, now=now, by=by)
    except KeyError:
        print("Nothing to undo" if last else "Action not found", file=stderr)
        return 3
    except UndoStateError as error:
        print("Already undone" if error.code == "undone" else "Undo window expired", file=stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False) if as_json else undo_text(result), file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(undo_main())
