"""Body-free JSONL records protected by a per-lane lock."""
import json
import os
import re
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path


def validate_lane(lane):
    if not isinstance(lane, str) or not re.fullmatch(r"[a-z0-9_-]+", lane):
        raise ValueError("Invalid lane")
    return lane


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
    deadline = time.monotonic() + 30
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
    with path.open("a+b") as handle:
        if os.name == "nt":
            if handle.seek(0, 2) == 0:
                handle.write(b"0")
                handle.flush()
            while True:
                try:
                    handle.seek(0)
                    locking.locking(handle.fileno(), locking.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Inbox lock unavailable") from None
                    time.sleep(0.01)
        else:
            locking.flock(handle.fileno(), locking.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                locking.locking(handle.fileno(), locking.LK_UNLCK, 1)
            else:
                locking.flock(handle.fileno(), locking.LOCK_UN)


def record(message, kind=None):
    from datetime import datetime, timezone
    return {"id": message.get("id", message.get("message_id")),
            "delivered_at": datetime.now(timezone.utc).isoformat(),
            "channel": message["channel"], "ts": message["ts"],
            "thread_ts": message.get("thread_ts"),
            "followup": bool(message.get("followup")),
            "kind": kind or ("followup" if message.get("followup") else "new")}


def append(root, lane, records):
    keys = ("id", "delivered_at", "channel", "ts", "thread_ts", "followup", "kind")
    clean = [{key: row[key] for key in keys} for row in records]
    for row in clean:
        if row["kind"] not in ("new", "followup", "reroute"):
            raise ValueError("Invalid inbox record kind")
        if any(not isinstance(row[k], str) or not row[k] for k in keys[:4]):
            raise ValueError("Invalid inbox record metadata")
    with lane_lock(root, lane):
        with (Path(root) / (lane + ".jsonl")).open("a", encoding="utf-8") as handle:
            for row in clean:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def read(root, lane, *, include_done=False, store=None):
    validate_lane(lane)
    lock = lane_lock(root, lane) if (Path(root) / (lane + ".lock")).exists() else nullcontext()
    with lock:
        path = Path(root) / (lane + ".jsonl")
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
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
