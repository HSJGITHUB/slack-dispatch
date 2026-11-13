"""Transactional SQLite ledger. Construction performs no I/O."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
import threading


@dataclass(frozen=True)
class RecordResult:
    message_id: str | None
    created: bool
    duplicate: bool
    kind: str


def now():
    return datetime.now().astimezone().isoformat()


class LedgerStore:
    def __init__(self, path, *, store_body=False):
        self.path = Path(path)
        self.store_body = store_body
        self._db = None
        self._lock = threading.RLock()

    def open(self):
        with self._lock:
            if self._db is not None:
                return
            self._db = sqlite3.connect(str(self.path), isolation_level=None,
                                       check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def _transaction(self):
        with self._lock:
            if self._db is None:
                raise RuntimeError("Ledger is not open")
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def migrate(self):
        with self._transaction() as db:
            # Execute statements individually: executescript commits implicitly.
            ddl = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
            statement = ""
            for line in ddl.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""
            versions = [r[0] for r in db.execute("SELECT version FROM schema_version")]
            if any(version not in (1, 2) for version in versions):
                raise RuntimeError("Unsupported ledger schema version")
            columns = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
            additions = {"event_id": "TEXT", "channel_type": "TEXT",
                         "has_files": "INTEGER NOT NULL DEFAULT 0", "edited_at": "TEXT",
                         "note": "TEXT", "suggestion_reason": "TEXT",
                         "attention": "TEXT", "is_bot": "INTEGER NOT NULL DEFAULT 0", "origin": "TEXT"}
            for name, declaration in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE messages ADD COLUMN {name} {declaration}")
            seen_columns = {r[1] for r in db.execute("PRAGMA table_info(events_seen)")}
            for name in ("source", "channel"):
                if name not in seen_columns:
                    db.execute(f"ALTER TABLE events_seen ADD COLUMN {name} TEXT")
            if not versions:
                db.execute("INSERT INTO schema_version(version) VALUES (2)")
            elif versions == [1]:
                db.execute("UPDATE schema_version SET version=2 WHERE version=1")

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def _rows(self, query, params=()):
        with self._lock:
            if self._db is None:
                raise RuntimeError("Ledger is not open")
            return [dict(row) for row in self._db.execute(query, params)]

    def record_event(self, event):
        source = event["source"]
        channel, ts = event.get("channel"), event.get("ts")
        mid = f"{source}:{channel}:{ts}" if channel and ts else None
        event_id = event.get("event_id")
        kind = event["event_type"]
        if kind in ("channel_created", "member_joined_channel"):
            kind, mid = "channel_meta", None
        received = event["received_at"]
        created, duplicate = False, False
        with self._transaction() as db:
            if event_id:
                seen = db.execute("SELECT * FROM events_seen WHERE event_id=?", (event_id,)).fetchone()
                if seen:
                    return RecordResult(seen["message_id"], False, True, seen["kind"])
            if kind == "message" and mid:
                exists = db.execute("SELECT 1 FROM messages WHERE id=?", (mid,)).fetchone()
                subtype = event.get("subtype")
                if subtype == "message_changed":
                    db.execute("UPDATE messages SET edited_at=?, followup=1, has_files=?, is_bot=? WHERE id=?",
                               (received, bool(event.get("has_files")),
                                bool(event.get("is_bot") or event.get("bot_id") or not event.get("user")), mid))
                    if self.store_body and "text" in event:
                        db.execute("UPDATE messages SET body=? WHERE id=?", (event["text"], mid))
                elif subtype == "message_deleted":
                    db.execute("UPDATE messages SET body=NULL, note='message_deleted', "
                               "followup=1, edited_at=? WHERE id=?", (received, mid))
                elif exists:
                    duplicate = True
                else:
                    db.execute("""INSERT INTO messages
                        (id, source, channel, ts, thread_ts, author, is_thread_reply,
                         received_at, state, body, event_id, channel_type, has_files, is_bot, origin)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?, ?)""",
                               (mid, source, channel, ts, event.get("thread_ts"), event.get("user"),
                                bool(event.get("is_thread_reply")), received,
                                event.get("text") if self.store_body else None,
                                event_id, event.get("channel_type"), bool(event.get("has_files")),
                                bool(event.get("is_bot") or event.get("bot_id") or not event.get("user")), event.get("origin")))
                    created = True
            elif kind in ("reaction_added", "reaction_removed") and mid:
                user, reaction = event.get("user"), event.get("reaction")
                if user and reaction:
                    if kind == "reaction_added":
                        db.execute("""INSERT INTO reactions VALUES (?, ?, ?, ?, NULL)
                            ON CONFLICT(message_id, user, reaction) DO UPDATE
                            SET added_at=excluded.added_at, removed_at=NULL""",
                                   (mid, user, reaction, received))
                    else:
                        db.execute("""INSERT INTO reactions VALUES (?, ?, ?, NULL, ?)
                            ON CONFLICT(message_id, user, reaction) DO UPDATE
                            SET removed_at=excluded.removed_at""", (mid, user, reaction, received))
            elif kind == "file_shared" and mid:
                db.execute("UPDATE messages SET has_files=1 WHERE id=?", (mid,))
            if event_id:
                db.execute("INSERT INTO events_seen (event_id, received_at, kind, message_id, source, channel) "
                           "VALUES (?, ?, ?, ?, ?, ?)",
                           (event_id, received, kind, mid, source, channel))
        return RecordResult(mid, created, duplicate, kind)

    def get_message(self, message_id, *, include_body=False):
        rows = self._rows("SELECT * FROM messages WHERE id=?", (message_id,))
        if not rows:
            return None
        if not include_body:
            rows[0].pop("body", None)
        return rows[0]

    def list_messages(self, *, state=None, lane=None, since=None, limit=100):
        clauses, params = [], []
        for column, value, op in (("state", state, "="), ("lane", lane, "="),
                                  ("received_at", since, ">=")):
            if value is not None:
                clauses.append(f"{column} {op} ?")
                params.append(value)
        if type(limit) is not int or limit < 0:
            raise ValueError("limit must be a nonnegative integer")
        query = "SELECT * FROM messages"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        rows = self._rows(query + " ORDER BY received_at, id LIMIT ?", (*params, limit))
        for row in rows:
            row.pop("body", None)
        return rows

    def _update(self, message_id, fields):
        with self._transaction() as db:
            assignments = ", ".join(f"{key}=?" for key in fields)
            db.execute(f"UPDATE messages SET {assignments} WHERE id=?",
                       (*fields.values(), message_id))

    @staticmethod
    def _state_fields(state):
        if state not in ("new", "assigned", "acked", "done", "human_pending", "excluded"):
            raise ValueError("Invalid message state")
        fields = {"state": state}
        stamp = {"assigned": "assigned_at", "acked": "ack_at", "done": "done_at"}.get(state)
        if stamp:
            fields[stamp] = now()
        return fields

    def set_route(self, message_id, lane, reason, *, state="assigned"):
        fields = self._state_fields(state)
        fields.update(lane=lane, route_reason=reason)
        self._update(message_id, fields)

    def set_state(self, message_id, state, *, note=None):
        fields = self._state_fields(state)
        if note is not None:
            fields["note"] = note
        self._update(message_id, fields)

    def set_body(self, message_id, body):
        """Fill a missing body (rehydration). Returns True when a row changed.

        Requires store_body. Rows that already hold a body or were deleted are unchanged.
        """
        if not self.store_body:
            return False
        with self._transaction() as db:
            changed = db.execute("UPDATE messages SET body=? WHERE id=? AND body IS NULL "
                                 "AND (note IS NULL OR note != 'message_deleted')",
                                 (body, message_id)).rowcount
        return changed > 0

    def set_suggestion(self, message_id, lane, reason, classifier_version):
        self._update(message_id, {"classifier_suggestion": lane, "suggestion_reason": reason,
                                  "classifier_version": classifier_version})

    def set_attention(self, message_id, attention):
        if attention not in (None, "always_human", "owner_mention"):
            raise ValueError("Invalid attention flag")
        self._update(message_id, {"attention": attention})

    def mark_followup(self, message_id):
        self._update(message_id, {"followup": 1})

    def bind_thread(self, source, channel, thread_ts, lane):
        with self._transaction() as db:
            db.execute("""INSERT INTO bindings VALUES (?, ?, ?, ?)
                ON CONFLICT(source, channel, thread_ts) DO UPDATE SET lane=excluded.lane""",
                       (source, channel, thread_ts, lane))

    def lookup_binding(self, source, channel, thread_ts):
        rows = self._rows("SELECT lane FROM bindings WHERE source=? AND channel=? AND thread_ts=?",
                          (source, channel, thread_ts))
        return rows[0]["lane"] if rows else None

    def upsert_lane(self, lane_id, *, session_pointer=None, paused=None):
        with self._transaction() as db:
            db.execute("INSERT OR IGNORE INTO lanes(id) VALUES (?)", (lane_id,))
            if session_pointer is not None:
                db.execute("UPDATE lanes SET session_pointer=? WHERE id=?", (session_pointer, lane_id))
            if paused is not None:
                db.execute("UPDATE lanes SET paused=? WHERE id=?", (bool(paused), lane_id))

    def get_lane(self, lane_id):
        rows = self._rows("SELECT * FROM lanes WHERE id=?", (lane_id,))
        return rows[0] if rows else None

    def list_lanes(self):
        return self._rows("SELECT * FROM lanes ORDER BY id")

    def get_cursor(self, source, channel):
        rows = self._rows("SELECT cursor FROM cursors WHERE source=? AND channel=?", (source, channel))
        return rows[0]["cursor"] if rows else None

    def set_cursor(self, source, channel, cursor):
        with self._transaction() as db:
            db.execute("""INSERT INTO cursors VALUES (?, ?, ?)
                ON CONFLICT(source, channel) DO UPDATE SET cursor=excluded.cursor""",
                       (source, channel, cursor))

    def list_channels(self, source="slack"):
        return [row["channel"] for row in self._rows("""SELECT channel FROM events_seen WHERE source=? AND channel IS NOT NULL
            UNION SELECT channel FROM messages WHERE source=?
            UNION SELECT channel FROM cursors WHERE source=?
            UNION SELECT channel FROM bindings WHERE source=? ORDER BY channel""", (source,) * 4)]

    def list_reactions(self, message_id):
        return self._rows("SELECT * FROM reactions WHERE message_id=? ORDER BY user, reaction",
                          (message_id,))

    def record_gap(self, source, detected_at, last_event_at=None, resumed_at=None, note=None):
        with self._transaction() as db:
            return db.execute("INSERT INTO ingest_gaps "
                              "(source, detected_at, last_event_at, resumed_at, note) VALUES (?, ?, ?, ?, ?)",
                              (source, detected_at, last_event_at, resumed_at, note)).lastrowid

    def complete_gaps(self, at):
        with self._transaction() as db:
            db.execute("UPDATE ingest_gaps SET catchup_at=? WHERE catchup_at IS NULL "
                       "AND resumed_at IS NOT NULL AND resumed_at<=?", (at, at))

    # Read helpers for the operator console (P10). Reads only; no schema change.

    def has_table(self, name):
        return bool(self._rows("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))

    def thread_messages(self, source, channel, thread_ts, *, include_body=False):
        """The thread root (ts == thread_ts) and its replies, oldest first by numeric ts."""
        rows = self._rows("SELECT * FROM messages WHERE source=? AND channel=? AND (ts=? OR thread_ts=?)",
                          (source, channel, thread_ts, thread_ts))
        for row in rows:
            if not include_body:
                row.pop("body", None)
        return sorted(rows, key=lambda row: (_numeric(row["ts"]), row["id"]))

    def search_messages(self, text, *, limit=200):
        """Rows whose id contains text, whose channel or author equals text, or whose stored
        body contains text; newest first by numeric ts. Bodies are included for display."""
        if type(limit) is not int or limit < 0:
            raise ValueError("limit must be a nonnegative integer")
        escaped = text.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        pattern = "%" + escaped + "%"
        rows = self._rows("SELECT * FROM messages WHERE id LIKE ? ESCAPE '!' OR channel=? OR author=? "
                          "OR (body IS NOT NULL AND body LIKE ? ESCAPE '!') "
                          "ORDER BY CAST(ts AS REAL) DESC, id DESC LIMIT ?",
                          (pattern, text, text, pattern, limit))
        return sorted(rows, key=lambda row: (_numeric(row["ts"]), row["id"]), reverse=True)

    def lane_activity(self, lane):
        """State and timestamp columns of every message owned by a lane (no bodies)."""
        return self._rows("SELECT id, state, received_at, assigned_at, ack_at, done_at "
                          "FROM messages WHERE lane=?", (lane,))

    def author_lane_counts(self, author, *, limit=100, exclude_id=None):
        """Lane counts over the author's most recent `limit` lane-owned messages."""
        return self._rows("SELECT lane, COUNT(*) AS count FROM (SELECT lane FROM messages "
                          "WHERE author=? AND lane IS NOT NULL AND id IS NOT ? "
                          "ORDER BY received_at DESC LIMIT ?) GROUP BY lane ORDER BY count DESC, lane",
                          (author, exclude_id, limit))

    def last_lane_delivery(self, lane):
        """Latest service_deliveries time (epoch seconds) for a lane, or None without the table."""
        if not self.has_table("service_deliveries"):
            return None
        rows = self._rows("SELECT MAX(delivered_at) AS at FROM service_deliveries WHERE lane=?", (lane,))
        return rows[0]["at"] if rows else None

    def last_lane_reminder(self, lane):
        """Latest unacknowledged-timer reminder (epoch seconds) sent for a lane's messages."""
        rows = self._rows("SELECT MAX(t.sent_at) AS at FROM timer_events t JOIN messages m "
                          "ON m.id = t.message_id WHERE t.timer='unacknowledged' AND m.lane=?", (lane,))
        return rows[0]["at"] if rows else None

    def list_gaps(self, limit=20):
        return self._rows("SELECT * FROM ingest_gaps ORDER BY id DESC LIMIT ?", (limit,))


def _numeric(ts):
    """Numeric Slack ts for ordering; unusable values sort first."""
    try:
        value = Decimal(str(ts))
        return value if value.is_finite() else Decimal("-Infinity")
    except (InvalidOperation, ValueError):
        return Decimal("-Infinity")
