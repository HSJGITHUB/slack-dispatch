"""Caller-driven, interval-limited, atomic heartbeat publication."""

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile
import time

SCHEMA = "slack-dispatch-heartbeat/1"


def _seconds(value):
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        return datetime.fromisoformat(value).timestamp()
    if isinstance(value, bool):
        raise ValueError("Boolean is not a timestamp")
    return float(value)


class HeartbeatWriter:
    def __init__(self, path, interval_seconds=30, clock=time.time):
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("interval_seconds must be finite and positive")
        self.path = Path(path)
        self.interval_seconds = interval_seconds
        self.clock = clock
        self._last = None

    def write(self, *, ok=True, status="running", version="unknown", counters=None,
              force=False):
        now = _seconds(self.clock())
        if not math.isfinite(now):
            raise ValueError("clock must return a finite timestamp")
        if (not force and self._last is not None
                and 0 <= now - self._last < self.interval_seconds):
            return False
        payload = dict(schema=SCHEMA, ts=datetime.fromtimestamp(now, timezone.utc).astimezone().isoformat(),
                       pid=os.getpid(), ok=bool(ok), status=str(status),
                       version=str(version), counters=dict(counters or {}))
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp",
                                         dir=self.path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        self._last = now
        return True


def read_heartbeat(path):
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) and value.get("schema") == SCHEMA else None
    except (OSError, ValueError, UnicodeError):
        return None


def is_stale(hb, now, max_age_seconds):
    if not math.isfinite(max_age_seconds) or max_age_seconds < 0:
        raise ValueError("max_age_seconds must be finite and nonnegative")
    try:
        if not hb or hb.get("schema") != SCHEMA:
            return True
        age = _seconds(now) - _seconds(hb["ts"])
        return not math.isfinite(age) or age < 0 or age > max_age_seconds
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError, OSError):
        return True
