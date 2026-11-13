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
"""
from slack_dispatch.inbox.files import append, record, validate_lane
from slack_dispatch.router.apply import record_scope_proposal

REASON = "human:console"
MAX_IDS = 200

QUEUE_ACTIONS = ("assign", "direct", "exclude", "done")
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


def bind_message_thread(store, message, lane):
    """Point the message's thread (its thread_ts, else its own ts) at the lane (P13)."""
    root = message.get("thread_ts") or message.get("ts")
    if message.get("channel") and root:
        store.bind_thread(message.get("source") or "slack", message["channel"], root, lane)


def assign(store, inbox_root, message_id, lane, *, note=None, reason=REASON, min_evidence=None):
    """CLI `assign ID LANE [--note]`: route, optional note, bind the thread to the lane,
    append a `new` inbox record, queue a lane wake (source = the reason without `human:`).
    Assigning a `human_pending` item records scope learning evidence (P13)."""
    message = _message(store, message_id)
    _lane(store, lane)
    refusal = inbox_refusal(dict(message, id=message_id))
    if refusal:
        raise ActionError("not_inbox_item", refusal)
    store.set_route(message_id, lane, reason)
    if note:
        store.set_state(message_id, "assigned", note=note)
    bind_message_thread(store, message, lane)
    append(inbox_root, lane, [record(dict(message, id=message_id), "new")])
    request_wake(store, lane, message_id, _source(reason))
    if message.get("state") == "human_pending":
        record_scope_proposal(store, message, lane, "assign", min_evidence=min_evidence)
    return message


def reroute(store, inbox_root, message_id, lane, *, source="console", min_evidence=None):
    """CLI `reroute ID --to LANE`: reason `reroute:<old lane>`, bind the thread to the new
    lane, append a `reroute` record, queue a lane wake. A reroute to another lane records
    scope learning evidence (P13)."""
    message = _message(store, message_id)
    _lane(store, lane)
    refusal = inbox_refusal(dict(message, id=message_id))
    if refusal:
        raise ActionError("not_inbox_item", refusal)
    store.set_route(message_id, lane, "reroute:" + str(message.get("lane") or "none"))
    bind_message_thread(store, message, lane)
    append(inbox_root, lane, [record(dict(message, id=message_id), "reroute")])
    request_wake(store, lane, message_id, source)
    if message.get("lane") != lane:
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


def queue_guard(message):
    """Triage actions apply only to items still waiting in the human queue."""
    if message["state"] != "human_pending":
        raise ActionError("conflict", "state is " + str(message["state"]))


def lane_guard(lane):
    """Lane inbox actions apply only to open items the lane still owns."""
    def check(message):
        if message.get("lane") not in (None, lane):
            raise ActionError("conflict", "owned by lane " + str(message.get("lane")))
        if message["state"] in ("done", "excluded"):
            raise ActionError("conflict", "state is " + str(message["state"]))
    return check


def apply(store, inbox_root, action, ids, *, lane=None, note=None, guard=None, min_evidence=None):
    """Apply one action to each unique id in order; return one result per id.

    `lane` is the destination for assign, direct and reroute. `note` is recorded by
    assign (when set) and done; done without a note records `human:console`. A guard
    may refuse an id with ActionError("conflict"). Failures never stop the batch.
    `min_evidence` is `scope_learning.min_evidence` for the learning events (P13).
    """
    results = []
    for message_id in dict.fromkeys(ids):
        try:
            message = store.get_message(message_id)
            if message is None:
                raise ActionError("not_found")
            if guard is not None:
                guard(message)
            if action in ("assign", "direct"):
                assign(store, inbox_root, message_id, lane, note=note, min_evidence=min_evidence)
            elif action == "reroute":
                reroute(store, inbox_root, message_id, lane, min_evidence=min_evidence)
            elif action == "exclude":
                exclude(store, message_id)
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
        results.append(dict(id=message_id, ok=True, error=None, detail=None))
    return results


def commit_status(result):
    """Map one `apply` result to a P14 commit status: applied, conflict, not_found or error."""
    if result["ok"]:
        return "applied"
    if result["error"] in ("conflict", "not_found"):
        return result["error"]
    return "error"
