"""Transactional SQLite ledger. Construction performs no I/O."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
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
            if any(version != 1 for version in versions):
                raise RuntimeError("Unsupported ledger schema version")
            columns = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
            additions = {"event_id": "TEXT", "channel_type": "TEXT",
                         "has_files": "INTEGER NOT NULL DEFAULT 0", "edited_at": "TEXT",
                         "note": "TEXT", "suggestion_reason": "TEXT",
                         "attention": "TEXT", "is_bot": "INTEGER NOT NULL DEFAULT 0"}
            for name, declaration in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE messages ADD COLUMN {name} {declaration}")
            seen_columns = {r[1] for r in db.execute("PRAGMA table_info(events_seen)")}
            for name in ("source", "channel"):
                if name not in seen_columns:
                    db.execute(f"ALTER TABLE events_seen ADD COLUMN {name} TEXT")
            if not versions:
                db.execute("INSERT INTO schema_version(version) VALUES (1)")

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
                         received_at, state, body, event_id, channel_type, has_files, is_bot)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?)""",
                               (mid, source, channel, ts, event.get("thread_ts"), event.get("user"),
                                bool(event.get("is_thread_reply")), received,
                                event.get("text") if self.store_body else None,
                                event_id, event.get("channel_type"), bool(event.get("has_files")),
                                bool(event.get("is_bot") or event.get("bot_id") or not event.get("user"))))
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
            UNION SELECT channel FROM cursors WHERE source=? ORDER BY channel""", (source,) * 3)]

    def list_reactions(self, message_id):
        return self._rows("SELECT * FROM reactions WHERE message_id=? ORDER BY user, reaction",
                          (message_id,))
