"""Resolve a lane session value (name, session id or id prefix) to a live session name.

The host writes one JSON file per live session under the sessions directory. Each
file holds at least `sessionId` and `name`. Session ids are stable; names are not.
Logs carry lane ids and counts only, never session values or names. Not-live and
ambiguous ids are logged per lane on state transitions, plus at most one reminder
line per lane every REMINDER_SECONDS while the lane stays undeliverable.
"""
import json
import logging
import re
import time
from pathlib import Path

LOG = logging.getLogger(__name__)

DEFAULT_SESSIONS_DIR = "~/.claude/sessions"
CACHE_SECONDS = 30
REMINDER_SECONDS = 1800
LIVE, NOT_LIVE, AMBIGUOUS = "live", "not_live", "ambiguous"
# A full uuid or a prefix of one: at least 8 hex characters, then hex or hyphens.
ID_FORM = re.compile(r"[0-9a-f]{8}[0-9a-f-]*", re.IGNORECASE)


def is_session_id(value):
    """True when the value is addressed by session id (full uuid or 8+ hex prefix)."""
    return isinstance(value, str) and ID_FORM.fullmatch(value) is not None


def scan_sessions(sessions_dir):
    """Return (sessionId, name) pairs from readable session files, in file name order."""
    root = Path(sessions_dir).expanduser()
    try:
        paths = sorted(root.glob("*.json"))
    except OSError:
        return []
    records = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        session_id, name = data.get("sessionId"), data.get("name")
        if isinstance(session_id, str) and session_id and isinstance(name, str) and name:
            records.append((session_id, name))
    return records


class SessionResolver:
    """Per-process resolver with a short scan cache shared by all session adapters."""

    def __init__(self, sessions_dir=None, *, clock=time.monotonic, ttl=CACHE_SECONDS,
                 reminder=REMINDER_SECONDS):
        self.sessions_dir = sessions_dir or DEFAULT_SESSIONS_DIR
        self.clock = clock
        self.ttl = ttl
        self.reminder = reminder
        self.lane_state = {}  # lane -> live, not_live or ambiguous (last observation)
        self.last_logged = {}  # lane -> clock value of the last not-live/ambiguous line
        self._skipped = set()  # distinct (lane, message id) pairs skipped as not live
        self._records = None
        self._scanned_at = None

    @property
    def skipped_no_session(self):
        """Distinct (lane, message id) pairs whose wake was skipped because the id was not live."""
        return len(self._skipped)

    def sessions(self):
        now = self.clock()
        if (self._records is None or now < self._scanned_at
                or now - self._scanned_at >= self.ttl):
            self._records = scan_sessions(self.sessions_dir)
            self._scanned_at = now
        return self._records

    def resolve_target(self, lane, value, *, warn_duplicates=True, message_ids=None):
        """Return the session name to address, or None to skip the wake.

        The delivery gate passes warn_duplicates=False so a duplicate name is
        reported once, by the adapter that sends. message_ids identifies the
        skipped items for the wake_skipped_no_session count; without it the
        lane alone is counted once.
        """
        if not value:
            return None
        if not is_session_id(value):
            self._observe(lane, LIVE)
            if warn_duplicates:
                self._warn_duplicates(lane, value, by_id=False)
            return value
        records = self.sessions()
        key = value.lower()
        matches = [record for record in records if record[0].lower() == key]
        if not matches:
            matches = [record for record in records if record[0].lower().startswith(key)]
        if not matches:
            for message_id in (None,) if message_ids is None else message_ids:
                self._skipped.add((lane, message_id))
            self._observe(lane, NOT_LIVE)
            return None
        count = len({record[0] for record in matches})
        if count > 1:
            self._observe(lane, AMBIGUOUS, count)
            return None
        self._observe(lane, LIVE)
        name = matches[0][1]
        if warn_duplicates:
            self._warn_duplicates(lane, name, by_id=True)
        return name

    def _observe(self, lane, state, count=0):
        """Log state transitions per lane and throttle repeats to one reminder per period."""
        previous = self.lane_state.get(lane)
        self.lane_state[lane] = state
        if state == LIVE:
            if previous in (NOT_LIVE, AMBIGUOUS):
                self.last_logged.pop(lane, None)
                LOG.info("session live again: lane %s", lane)
            return
        now = self.clock()
        reminder = state == previous
        if reminder:
            last = self.last_logged.get(lane)
            if last is not None and 0 <= now - last < self.reminder:
                return
        self.last_logged[lane] = now
        suffix = " (reminder)" if reminder else ""
        if state == NOT_LIVE:
            LOG.info("session not live: lane %s%s", lane, suffix)
        else:
            LOG.warning("ambiguous session id: lane %s matches %d live sessions%s",
                        lane, count, suffix)

    def _warn_duplicates(self, lane, name, *, by_id):
        count = len({session_id for session_id, other in self.sessions() if other == name})
        if count > 1:
            LOG.warning("duplicate session name: lane %s, %d live sessions share the name "
                        "(lane addressed by %s)", lane, count, "id" if by_id else "name")
