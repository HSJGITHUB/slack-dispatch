"""Console write actions.

Each function performs the same LedgerStore calls and inbox append as the inbox CLI
command of the same name (`slack_dispatch/inbox/cli.py`), so the ledger and the lane
inbox files change exactly as they would from the CLI. Where the CLI records
`human:cli`, the console records `human:console`. `exclude` has no CLI command; it
uses the same call the router uses for exclusions (state `excluded`, reason in note).

Nothing here wakes a session or posts anywhere. The service's existing timers act on
the new ledger state (see docs/CONSOLE.md).
"""
from slack_dispatch.inbox.files import append, record, validate_lane

REASON = "human:console"
MAX_IDS = 200

QUEUE_ACTIONS = ("assign", "direct", "exclude", "done")
LANE_ACTIONS = ("ack", "done", "reroute")


class ActionError(Exception):
    """A per-message failure with a stable code: not_found, lane_not_found or conflict."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _message(store, message_id):
    message = store.get_message(message_id)
    if message is None:
        raise ActionError("not_found")
    return message


def _lane(store, lane):
    validate_lane(lane)
    if store.get_lane(lane) is None:
        raise ActionError("lane_not_found")


def assign(store, inbox_root, message_id, lane, *, note=None, reason=REASON):
    """CLI `assign ID LANE [--note]`: route, optional note, append a `new` inbox record."""
    message = _message(store, message_id)
    _lane(store, lane)
    store.set_route(message_id, lane, reason)
    if note:
        store.set_state(message_id, "assigned", note=note)
    append(inbox_root, lane, [record(dict(message, id=message_id), "new")])
    return message


def reroute(store, inbox_root, message_id, lane):
    """CLI `reroute ID --to LANE`: reason `reroute:<old lane>`, append a `reroute` record."""
    message = _message(store, message_id)
    _lane(store, lane)
    store.set_route(message_id, lane, "reroute:" + str(message.get("lane") or "none"))
    append(inbox_root, lane, [record(dict(message, id=message_id), "reroute")])
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


def apply(store, inbox_root, action, ids, *, lane=None, note=None, guard=None):
    """Apply one action to each unique id in order; return one result per id.

    `lane` is the destination for assign, direct and reroute. `note` is recorded by
    assign (when set) and done; done without a note records `human:console`. A guard
    may refuse an id with ActionError("conflict"). Failures never stop the batch.
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
                assign(store, inbox_root, message_id, lane, note=note)
            elif action == "reroute":
                reroute(store, inbox_root, message_id, lane)
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
