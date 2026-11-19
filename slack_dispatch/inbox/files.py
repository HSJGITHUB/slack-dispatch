"""Body-free JSONL records protected by a per-lane lock."""
import errno
import json
import os
import re
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

# Total wait for the lane lock itself (the existing 30 second bound).
LOCK_TIMEOUT = 30.0
# Total wait for a transient Windows sharing or lock violation on an inbox file.
SHARING_TIMEOUT = 5.0
RETRY_SLEEP = 0.01
# ERROR_SHARING_VIOLATION and ERROR_LOCK_VIOLATION: another handle holds the file
# or the byte range. Any other OSError (for example ERROR_ACCESS_DENIED) is not retried.
_TRANSIENT_WINERRORS = (32, 33)
# msvcrt.locking reports a byte range held by another process as EACCES (LK_NBLCK)
# or EDEADLOCK (LK_LOCK after its own retries).
_LOCK_BUSY_ERRNOS = (errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK))


def validate_lane(lane):
    if not isinstance(lane, str) or not re.fullmatch(r"[a-z0-9_-]+", lane):
        raise ValueError("Invalid lane")
    return lane


def _is_transient(err):
    return isinstance(err, OSError) and getattr(err, "winerror", None) in _TRANSIENT_WINERRORS


def _retry_transient(action, what, timeout=None):
    """Run `action`, waiting out transient Windows sharing or lock violations.

    Retries only errors whose winerror is a sharing or lock violation, with short sleeps,
    for at most `timeout` seconds (default SHARING_TIMEOUT), then raises TimeoutError
    naming `what`. Other errors propagate unchanged. On POSIX `winerror` is never set,
    so this is a single call.
    """
    deadline = time.monotonic() + (SHARING_TIMEOUT if timeout is None else timeout)
    while True:
        try:
            return action()
        except OSError as err:
            if not _is_transient(err):
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Inbox file busy: {what}") from err
            time.sleep(RETRY_SLEEP)


@contextmanager
def lane_lock(root, lane):
    validate_lane(lane)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / (lane + ".lock")
    try:
        if os.name == "nt":
            import msvcrt as locking
        else:
            import fcntl as locking
    except ImportError:
        locking = None
    deadline = time.monotonic() + LOCK_TIMEOUT
    if locking is None:
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Inbox lock unavailable") from None
                time.sleep(0.01)
        try:
            yield
        finally:
            os.close(fd)
            # Preserve the lock artifact while releasing the atomic-create name.
            os.replace(path, root / (lane + ".lock.released"))
        return
    if os.name != "nt":
        with path.open("a+b") as handle:
            locking.flock(handle.fileno(), locking.LOCK_EX)
            try:
                yield
            finally:
                locking.flock(handle.fileno(), locking.LOCK_UN)
        return
    with _retry_transient(lambda: path.open("a+b"), path.name) as handle:
        # P33: lock first, write second. The lock covers byte 0, which Windows allows
        # beyond the end of file, so an empty lock file needs no content to be locked.
        # Writing the marker byte before holding the lock raced with another process
        # that had just locked byte 0 and failed with PermissionError (lock violation).
        while True:
            try:
                handle.seek(0)
                locking.locking(handle.fileno(), locking.LK_NBLCK, 1)
                break
            except OSError as err:
                if err.errno not in _LOCK_BUSY_ERRNOS:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("Inbox lock unavailable") from None
                time.sleep(RETRY_SLEEP)
        try:
            if handle.seek(0, 2) == 0:
                # Keep the one-byte lock file format; only the lock holder writes it.
                handle.write(b"0")
                handle.flush()
            yield
        finally:
            handle.seek(0)
            locking.locking(handle.fileno(), locking.LK_UNLCK, 1)


def valid_edit_delta(value):
    """P37: an edit size hint is {"added": int, "removed": int}, both non-negative."""
    return (isinstance(value, dict) and set(value) == {"added", "removed"}
            and all(type(value[key]) is int and value[key] >= 0 for key in value))


def record(message, kind=None):
    """Body-free inbox record.

    Without an explicit kind, `followup` is written only for an edited message (ledger
    edited_at, set by message_changed). A reply delivered soon after an earlier delivery
    to its thread keeps its ordinary kind (`new` or `backfill`) with followup: true.
    P37: a followup record also carries `edit_delta` ({"added": n, "removed": m}, token
    counts only) when the message passed one (the pipeline does for a re-delivered edit).
    """
    from datetime import datetime, timezone
    if not kind:
        kind = ("followup" if message.get("edited_at")
                else "backfill" if message.get("origin") == "reconcile" else "new")
    row = {"id": message.get("id", message.get("message_id")),
           "delivered_at": datetime.now(timezone.utc).isoformat(),
           "channel": message["channel"], "ts": message["ts"],
           "thread_ts": message.get("thread_ts"),
           "followup": bool(message.get("followup")),
           "kind": kind}
    delta = message.get("edit_delta")
    if kind == "followup" and valid_edit_delta(delta):
        row["edit_delta"] = {"added": delta["added"], "removed": delta["removed"]}
    return row


def append(root, lane, records):
    keys = ("id", "delivered_at", "channel", "ts", "thread_ts", "followup", "kind")
    clean = []
    for row in records:
        item = {key: row[key] for key in keys}
        if "edit_delta" in row:
            # P37: the only optional field; never any message text.
            if item["kind"] != "followup" or not valid_edit_delta(row["edit_delta"]):
                raise ValueError("Invalid inbox record edit_delta")
            item["edit_delta"] = {"added": row["edit_delta"]["added"],
                                  "removed": row["edit_delta"]["removed"]}
        clean.append(item)
    for row in clean:
        if row["kind"] not in ("new", "followup", "reroute", "backfill"):
            raise ValueError("Invalid inbox record kind")
        if any(not isinstance(row[k], str) or not row[k] for k in keys[:4]):
            raise ValueError("Invalid inbox record metadata")
    with lane_lock(root, lane):
        path = Path(root) / (lane + ".jsonl")
        with _retry_transient(lambda: path.open("a", encoding="utf-8"), path.name) as handle:
            for row in clean:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _read_rows(root, lane, locked):
    with lane_lock(root, lane) if locked else nullcontext():
        path = Path(root) / (lane + ".jsonl")
        if not path.exists():
            return []
        text = _retry_transient(lambda: path.read_text(encoding="utf-8"), path.name)
        return [json.loads(line) for line in text.splitlines()]


def read(root, lane, *, include_done=False, store=None):
    validate_lane(lane)
    lock_path = Path(root) / (lane + ".lock")
    rows = None
    if not lock_path.exists():
        try:
            rows = _read_rows(root, lane, False)
        except ValueError:
            if not lock_path.exists():
                raise
        if lock_path.exists():
            # A first writer created the lock while this unlocked read ran, so the read may
            # have seen a partly written append. Read again under the lock.
            rows = None
    if rows is None:
        rows = _read_rows(root, lane, True)
    result = []
    for row in rows:
        if store is not None:
            message = store.get_message(row["id"])
            if message:
                if message.get("lane") not in (None, lane):
                    continue
                row.update({k: message[k] for k in ("state", "followup") if k in message})
        if include_done or row.get("state") != "done":
            result.append(row)
    return result
