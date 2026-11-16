"""Resolve a lane session value (name, session id or id prefix) to a live session name.

The host writes one JSON file per live session under the sessions directory. Each
file holds at least `sessionId` and `name`. Session ids are stable; names are not.
Logs carry lane ids and counts only, never session values or names. Not-live and
ambiguous ids are logged per lane on state transitions, plus at most one reminder
line per lane every REMINDER_SECONDS while the lane stays undeliverable.

P21: an optional Markdown sessions table (`console.sessions_table`) names sessions by the
title the operator sees in the IDE session list. `SessionTable` parses rows of the form
`| <last used> | <state> | <title> | <size> | `claude --resume <uuid>` |`, indexes them by
session id (looked up by full id or unique prefix) and parses the file again only when its
modification time or size changes. `SessionResolver.describe(value)` combines the live
session files with the table without logging or changing the per-lane state.
"""
import json
import logging
import os
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
# P21 sessions table: the last cell holds the resume command with the session id.
RESUME_CELL = re.compile(r"`?\s*claude\s+--resume\s+([0-9a-f]{8}[0-9a-f-]*)\s*`?", re.IGNORECASE)
TABLE_MAX_BYTES = 4 << 20
NO_SESSION, PAUSED = "no_session", "paused"


def parse_sessions_table(text):
    """{session id (lower case): row} from Markdown table lines; malformed rows are skipped.

    A row is `| <last used> | <state> | <title> | <size> | `claude --resume <uuid>` |`.
    Cells are trimmed; a title that contains `|` (escaped as `\\|` or not) is kept whole.
    Each row is `{id, title, state, last_used, size}` with empty cells as None. The first
    row of an id wins (such tables list the most recent use first).
    """
    rows = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if len(line) < 2 or not (line.startswith("|") and line.endswith("|")):
            continue
        raw = line[1:-1].split("|")
        cells = [cell.strip() for cell in raw]
        if len(cells) < 5:
            continue
        match = RESUME_CELL.fullmatch(cells[-1])
        if not match:
            continue
        session_id = match.group(1).lower()
        if session_id in rows:
            continue
        title = "|".join(raw[2:-2]).replace("\\|", "|").strip()
        rows[session_id] = {"id": session_id, "title": title or None, "state": cells[1] or None,
                            "last_used": cells[0] or None, "size": cells[-2] or None}
    return rows


class SessionTable:
    """The optional sessions table file, parsed again only when its mtime or size changes.

    A missing, unreadable or oversized file reads as an empty table (no titles).
    """

    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.parses = 0  # how many times the file was parsed (tests check the cache)
        self._key = None
        self._rows = {}

    def rows(self):
        try:
            info = os.stat(self.path)
        except OSError:
            self._key, self._rows = None, {}
            return self._rows
        key = (info.st_mtime_ns, info.st_size)
        if key != self._key:
            rows = {}
            if info.st_size <= TABLE_MAX_BYTES:
                try:
                    rows = parse_sessions_table(self.path.read_text(encoding="utf-8-sig"))
                except (OSError, UnicodeError):
                    rows = {}
            self.parses += 1
            self._key, self._rows = key, rows
        return self._rows

    def lookup(self, value):
        """The row of a full session id or of a prefix matching exactly one id, else None."""
        if not is_session_id(value):
            return None
        rows = self.rows()
        key = value.lower()
        if key in rows:
            return rows[key]
        matches = [row for session_id, row in rows.items() if session_id.startswith(key)]
        return matches[0] if len(matches) == 1 else None


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
                 reminder=REMINDER_SECONDS, sessions_table=None):
        self.sessions_dir = sessions_dir or DEFAULT_SESSIONS_DIR
        # P21: the operator's sessions table (a path or a SessionTable), else None.
        if sessions_table is None or isinstance(sessions_table, SessionTable):
            self.table = sessions_table
        else:
            self.table = SessionTable(sessions_table)
        self.clock = clock
        self.ttl = ttl
        self.reminder = reminder
        self.lane_state = {}  # lane -> live, not_live or ambiguous (last observation)
        self.last_logged = {}  # lane -> clock value of the last not-live/ambiguous line
        self._skipped = set()  # distinct (lane, message id) pairs skipped as not live
        self.resolved = {}  # lane -> (session id, name) of the last id-form resolution (P18)
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
        self.resolved[lane] = (matches[0][0], name)
        if warn_duplicates:
            self._warn_duplicates(lane, name, by_id=True)
        return name

    def resolved_session(self, lane, target):
        """(session id or None, name) of the session a delivery addressed (P18 delivery log).

        An id-form lane reports the id it resolved to. A name (or the human queue operator
        target) reports the id of the one live session with that name, else None.
        """
        record = self.resolved.get(lane)
        if record is not None and record[1] == target:
            return record
        ids = {session_id for session_id, name in self.sessions() if name == target}
        return (ids.pop() if len(ids) == 1 else None), target

    def title(self, value):
        """The sessions table title of a session id or id prefix, else None (P21)."""
        if self.table is None or not value:
            return None
        row = self.table.lookup(value)
        return row["title"] if row else None

    def describe(self, session_id, *, paused=False):
        """Identity of a lane session value for display (P21); no logging, no lane state.

        Returns `{id8, id, value, addressed_by, name, live, title, state, table_state}`.
        `name` is the live session name (None when not live), `live` whether one live
        session file matches (an id prefix matching several live sessions is not live),
        `title` the sessions table title (None without a table row), `table_state` the
        table's state cell. `state` is `no_session` without a value, `paused` for a paused
        lane, else `live`, `not_live` or `ambiguous`. `id8` is the first 8 characters of the
        session id when one is known.
        """
        result = {"id8": None, "id": None, "value": session_id or None, "addressed_by": None,
                  "name": None, "live": False, "title": None, "state": NO_SESSION,
                  "table_state": None}
        if not session_id:
            return result
        records = self.sessions()
        state = NOT_LIVE
        by_id = is_session_id(session_id)
        if by_id:
            result["addressed_by"] = "id"
            key = session_id.lower()
            matches = [record for record in records if record[0].lower() == key]
            if not matches:
                matches = [record for record in records if record[0].lower().startswith(key)]
            ids = {record[0] for record in matches}
            if len(ids) == 1:
                state = LIVE
                result.update(live=True, id=matches[0][0], name=matches[0][1])
            elif ids:
                state = AMBIGUOUS
        else:
            result["addressed_by"] = "name"
            ids = {record_id for record_id, name in records if name == session_id}
            if ids:
                state = LIVE
                result.update(live=True, name=session_id)
                if len(ids) == 1:
                    result["id"] = ids.pop()
        lookup = result["id"] or (session_id if by_id else None)
        row = self.table.lookup(lookup) if self.table is not None and lookup else None
        if row is not None:
            result.update(title=row["title"], table_state=row["state"])
            result["id"] = result["id"] or row["id"]
        ident = result["id"] or (session_id if by_id else None)
        result["id8"] = ident[:8].lower() if ident else None
        result["state"] = PAUSED if paused else state
        return result

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
