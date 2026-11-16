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


class ProposalStateError(Exception):
    """A scope proposal decision needs an open or suggested proposal (P13)."""


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
                         "attention": "TEXT", "is_bot": "INTEGER NOT NULL DEFAULT 0", "origin": "TEXT",
                         "deleted_at": "TEXT"}
            for name, declaration in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE messages ADD COLUMN {name} {declaration}")
            seen_columns = {r[1] for r in db.execute("PRAGMA table_info(events_seen)")}
            for name in ("source", "channel"):
                if name not in seen_columns:
                    db.execute(f"ALTER TABLE events_seen ADD COLUMN {name} TEXT")
            # P12: wake requests from the CLI, the console and card decisions (additive).
            db.execute("""CREATE TABLE IF NOT EXISTS wake_requests (
                id INTEGER PRIMARY KEY, lane TEXT, message_id TEXT, requested_at TEXT,
                source TEXT, consumed_at TEXT)""")
            # P15: membership cache and console channel exclusions (additive).
            db.execute("""CREATE TABLE IF NOT EXISTS channel_membership (
                channel TEXT PRIMARY KEY, kind TEXT, name TEXT, partner_user TEXT,
                is_member INTEGER, seen_at TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS channel_exclusions (
                id INTEGER PRIMARY KEY, channel TEXT NOT NULL, excluded_at TEXT, by TEXT,
                removed_at TEXT)""")
            # P16: users.info answers for the bot flag (additive).
            db.execute("""CREATE TABLE IF NOT EXISTS user_directory (
                user TEXT PRIMARY KEY, is_bot INTEGER, is_app_user INTEGER, deleted INTEGER,
                name TEXT, seen_at TEXT)""")
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
                               (received, bool(event.get("has_files")), event_is_bot(event), mid))
                    if self.store_body and "text" in event:
                        db.execute("UPDATE messages SET body=? WHERE id=?", (event["text"], mid))
                elif subtype == "message_deleted":
                    # P12: deleted_at marks the row; an open row is closed. Text is never kept.
                    db.execute("UPDATE messages SET body=NULL, note='message_deleted', "
                               "followup=1, edited_at=?, deleted_at=? WHERE id=?",
                               (received, received, mid))
                    db.execute("UPDATE messages SET state='done', done_at=? WHERE id=? "
                               "AND state IN ('new', 'assigned', 'acked', 'human_pending')",
                               (received, mid))
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
                                event_is_bot(event), event.get("origin")))
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

    # P12 helpers: lane cutover reads and the wake request queue.

    def lane_messages(self, lane):
        """Metadata of every message owned by a lane (no bodies), for the cutover plan."""
        return self._rows("SELECT id, source, channel, ts, thread_ts, state, received_at, "
                          "assigned_at, ack_at, done_at, note, route_reason, attention, deleted_at "
                          "FROM messages WHERE lane=? ORDER BY received_at, id", (lane,))

    def thread_delivery(self, source, channel, thread_ts, lane):
        """service_deliveries time (epoch seconds) of a thread and lane, or None."""
        if not self.has_table("service_deliveries"):
            return None
        rows = self._rows("SELECT delivered_at FROM service_deliveries "
                          "WHERE source=? AND channel=? AND thread_ts=? AND lane=?",
                          (source, channel, thread_ts, lane))
        return rows[0]["delivered_at"] if rows else None

    def request_wake(self, lane, message_id, source):
        """Queue one lane wake for a delivered message; the service consumes it."""
        with self._transaction() as db:
            return db.execute("INSERT INTO wake_requests (lane, message_id, requested_at, source) "
                              "VALUES (?, ?, ?, ?)", (lane, message_id, now(), source)).lastrowid

    def pending_wake_requests(self, limit=500):
        return self._rows("SELECT * FROM wake_requests WHERE consumed_at IS NULL "
                          "ORDER BY id LIMIT ?", (limit,))

    def consume_wake_requests(self, ids, at=None):
        stamp = at or now()
        with self._transaction() as db:
            for request_id in ids:
                db.execute("UPDATE wake_requests SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
                           (stamp, request_id))

    # P13 lane scope: claims, learned proposals and recorded sends.

    @staticmethod
    def _active_claim(db, lane, kind, key):
        return db.execute("SELECT * FROM scope_claims WHERE lane=? AND kind=? AND key=? "
                          "AND removed_at IS NULL ORDER BY id DESC LIMIT 1", (lane, kind, key)).fetchone()

    def add_scope_claim(self, lane, kind, key, *, note=None, claimed_by="cli", confirmed_by=None):
        """Record the lane's claim, or refresh its note and claimed_at when it is already active.

        Returns True when a new claim row was created. `confirmed_by` creates the claim as
        confirmed (thread claims, whose binding is written at the same time).
        """
        stamp = now()
        with self._transaction() as db:
            row = self._active_claim(db, lane, kind, key)
            if row is not None:
                db.execute("UPDATE scope_claims SET note=COALESCE(?, note), claimed_at=? WHERE id=?",
                           (note, stamp, row["id"]))
                return False
            db.execute("INSERT INTO scope_claims (lane, kind, key, note, claimed_at, claimed_by, status, "
                       "confirmed_at, confirmed_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (lane, kind, key, note, stamp, claimed_by,
                        "confirmed" if confirmed_by else "claimed",
                        stamp if confirmed_by else None, confirmed_by))
            return True

    def remove_scope_claim(self, lane, kind, key):
        """Mark the lane's active claim removed (rows are never deleted). False when none."""
        with self._transaction() as db:
            return db.execute("UPDATE scope_claims SET removed_at=? WHERE lane=? AND kind=? AND key=? "
                              "AND removed_at IS NULL", (now(), lane, kind, key)).rowcount > 0

    def list_scope_claims(self, lane=None, *, include_removed=False):
        query = "SELECT * FROM scope_claims WHERE (? IS NULL OR lane=?)"
        if not include_removed:
            query += " AND removed_at IS NULL"
        return self._rows(query + " ORDER BY lane, kind, key, id", (lane, lane))

    def scope_claim_lanes(self, kind, key):
        """Every lane that ever claimed (kind, key), removed claims included."""
        return {row["lane"] for row in self._rows(
            "SELECT DISTINCT lane FROM scope_claims WHERE kind=? AND key=?", (kind, key))}

    def lookup_scope_claim(self, kind, key):
        """The confirmed, active claim for (kind, key) that routing uses, or None."""
        rows = self._rows("SELECT lane, kind, key, confirmed_by, confirmed_at FROM scope_claims "
                          "WHERE kind=? AND key=? AND status='confirmed' AND removed_at IS NULL "
                          "ORDER BY confirmed_at DESC, id DESC LIMIT 1", (kind, key))
        return rows[0] if rows else None

    def confirmed_scope_claims(self):
        """Active confirmed channel, dm and person claims: the learned routing overrides."""
        return self._rows("SELECT * FROM scope_claims WHERE status='confirmed' AND removed_at IS NULL "
                          "AND kind IN ('channel', 'dm', 'person') ORDER BY kind, key, id")

    @classmethod
    def _confirm_claim(cls, db, lane, kind, key, by, stamp, note=None):
        db.execute("UPDATE scope_claims SET status='claimed' WHERE kind=? AND key=? AND lane!=? "
                   "AND status='confirmed' AND removed_at IS NULL", (kind, key, lane))
        row = cls._active_claim(db, lane, kind, key)
        if row is None:
            db.execute("INSERT INTO scope_claims (lane, kind, key, note, claimed_at, claimed_by, status, "
                       "confirmed_at, confirmed_by) VALUES (?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)",
                       (lane, kind, key, note, stamp, by, stamp, by))
        else:
            db.execute("UPDATE scope_claims SET status='confirmed', confirmed_at=?, confirmed_by=? "
                       "WHERE id=?", (stamp, by, row["id"]))

    def confirm_scope_claim(self, lane, kind, key, *, by, note=None):
        """Make the lane's claim the routing override for (kind, key); confirmed claims of
        other lanes for the same key return to `claimed`. Creates the claim when missing."""
        with self._transaction() as db:
            self._confirm_claim(db, lane, kind, key, by, now(), note)

    @staticmethod
    def _refresh_proposals(db, kind, key, min_evidence):
        """Recompute conflict flags and the open or suggested status for one key."""
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM scope_proposals WHERE kind=? AND key=?", (kind, key))]
        live = {row["lane"] for row in rows if row["status"] in ("open", "suggested", "confirmed")}
        for row in rows:
            conflict = row["status"] in ("open", "suggested", "confirmed") and bool(live - {row["lane"]})
            status = row["status"]
            if status in ("open", "suggested"):
                ready = row["evidence_count"] >= min_evidence or bool(row["imported"])
                status = "suggested" if ready and not conflict else "open"
            db.execute("UPDATE scope_proposals SET status=?, conflict=? WHERE kind=? AND key=? AND lane=?",
                       (status, int(conflict), kind, key, row["lane"]))

    def get_scope_proposal(self, kind, key, lane):
        rows = self._rows("SELECT * FROM scope_proposals WHERE kind=? AND key=? AND lane=?",
                          (kind, key, lane))
        return rows[0] if rows else None

    def list_scope_proposals(self, statuses=None):
        rows = self._rows("SELECT * FROM scope_proposals ORDER BY kind, key, lane")
        return [row for row in rows if statuses is None or row["status"] in statuses]

    def record_scope_evidence(self, kind, key, lane, source, *, min_evidence=2):
        """Count one event for (kind, key, lane) and return the proposal row.

        Deferred and superseded proposals reopen on new evidence; rejected and confirmed
        ones keep their status. Routing never reads proposals.
        """
        stamp = now()
        with self._transaction() as db:
            db.execute("INSERT INTO scope_proposals (kind, key, lane, evidence_count, first_seen, "
                       "last_seen, last_evidence) VALUES (?, ?, ?, 1, ?, ?, ?) "
                       "ON CONFLICT(kind, key, lane) DO UPDATE SET "
                       "evidence_count=evidence_count+1, last_seen=excluded.last_seen, "
                       "last_evidence=excluded.last_evidence, status=CASE WHEN status IN "
                       "('deferred', 'superseded') THEN 'open' ELSE status END",
                       (kind, key, lane, stamp, stamp, source))
            self._refresh_proposals(db, kind, key, min_evidence)
            return dict(db.execute("SELECT * FROM scope_proposals WHERE kind=? AND key=? AND lane=?",
                                   (kind, key, lane)).fetchone())

    def import_scope_proposal(self, kind, key, lane, *, min_evidence=2):
        """Load one harvested proposal as `suggested` (open while in conflict). Returns `added`,
        or `kept` when it was already imported, confirmed or rejected."""
        stamp = now()
        with self._transaction() as db:
            row = db.execute("SELECT * FROM scope_proposals WHERE kind=? AND key=? AND lane=?",
                             (kind, key, lane)).fetchone()
            if row is not None and (row["imported"] or row["status"] in ("confirmed", "rejected")):
                return "kept"
            if row is None:
                db.execute("INSERT INTO scope_proposals (kind, key, lane, evidence_count, first_seen, "
                           "last_seen, last_evidence, imported) VALUES (?, ?, ?, 1, ?, ?, 'import', 1)",
                           (kind, key, lane, stamp, stamp))
            else:
                db.execute("UPDATE scope_proposals SET imported=1, evidence_count=evidence_count+1, "
                           "last_seen=?, last_evidence='import', status=CASE WHEN status IN "
                           "('deferred', 'superseded') THEN 'open' ELSE status END "
                           "WHERE kind=? AND key=? AND lane=?", (stamp, kind, key, lane))
            self._refresh_proposals(db, kind, key, min_evidence)
            return "added"

    def decide_scope_proposal(self, kind, key, lane, decision, *, by="console", min_evidence=2):
        """Operator decision on an open or suggested proposal: confirm, reject or defer.

        confirm writes the confirmed claim (the routing override) and supersedes any other
        confirmed proposal for the key; reject returns the lane's confirmed claim (for
        example a scope:auto one) to `claimed`. Raises KeyError for an unknown proposal and
        ProposalStateError when it is not open or suggested. Returns the updated row.
        """
        status = {"confirm": "confirmed", "reject": "rejected", "defer": "deferred"}.get(decision)
        if status is None:
            raise ValueError("Unsupported proposal decision")
        stamp = now()
        with self._transaction() as db:
            row = db.execute("SELECT * FROM scope_proposals WHERE kind=? AND key=? AND lane=?",
                             (kind, key, lane)).fetchone()
            if row is None:
                raise KeyError(key)
            if row["status"] not in ("open", "suggested"):
                raise ProposalStateError(row["status"])
            if decision == "confirm":
                db.execute("UPDATE scope_proposals SET status='superseded', decided_at=? WHERE kind=? "
                           "AND key=? AND lane!=? AND status='confirmed'", (stamp, kind, key, lane))
                self._confirm_claim(db, lane, kind, key, by, stamp, note="confirmed proposal")
            elif decision == "reject":
                db.execute("UPDATE scope_claims SET status='claimed' WHERE lane=? AND kind=? AND key=? "
                           "AND status='confirmed' AND removed_at IS NULL", (lane, kind, key))
            db.execute("UPDATE scope_proposals SET status=?, decided_at=? WHERE kind=? AND key=? AND lane=?",
                       (status, stamp, kind, key, lane))
            self._refresh_proposals(db, kind, key, min_evidence)
            return dict(db.execute("SELECT * FROM scope_proposals WHERE kind=? AND key=? AND lane=?",
                                   (kind, key, lane)).fetchone())

    def record_send(self, channel, ts, thread_ts, lane, kind=None, *, source="slack"):
        """Record a lane's own post and bind its thread (thread_ts, else ts) to the lane in one
        transaction. A repeated (channel, ts) changes nothing. Returns (created, previous lane)."""
        root = thread_ts or ts
        with self._transaction() as db:
            created = db.execute("INSERT OR IGNORE INTO sends (channel, ts, thread_ts, lane, kind, "
                                 "recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                                 (channel, ts, thread_ts, lane, kind, now())).rowcount > 0
            previous = db.execute("SELECT lane FROM bindings WHERE source=? AND channel=? AND thread_ts=?",
                                  (source, channel, root)).fetchone()
            previous = previous["lane"] if previous else None
            if created:
                db.execute("""INSERT INTO bindings VALUES (?, ?, ?, ?)
                    ON CONFLICT(source, channel, thread_ts) DO UPDATE SET lane=excluded.lane""",
                           (source, channel, root, lane))
            return created, previous

    def list_sends(self, lane=None):
        return self._rows("SELECT * FROM sends WHERE (? IS NULL OR lane=?) "
                          "ORDER BY recorded_at, channel, ts", (lane, lane))

    # P15 membership cache and channel exclusions.

    def replace_membership(self, rows, seen_at):
        """Store one users.conversations result: listed rows become members (seen_at
        updated); rows no longer listed keep their name with is_member=0."""
        with self._transaction() as db:
            db.execute("UPDATE channel_membership SET is_member=0")
            for row in rows:
                db.execute("""INSERT INTO channel_membership VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(channel) DO UPDATE SET kind=excluded.kind, name=excluded.name,
                    partner_user=excluded.partner_user, is_member=excluded.is_member,
                    seen_at=excluded.seen_at""",
                           (row["channel"], row.get("kind"), row.get("name"), row.get("partner_user"),
                            int(bool(row.get("is_member", 1))), seen_at))

    def list_membership(self):
        if not self.has_table("channel_membership"):
            return []
        return self._rows("SELECT * FROM channel_membership ORDER BY channel")

    def add_channel_exclusion(self, channel, by):
        """Record an active ledger exclusion for the channel. False when one is already active."""
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM channel_exclusions WHERE channel=? AND removed_at IS NULL",
                          (channel,)).fetchone():
                return False
            db.execute("INSERT INTO channel_exclusions (channel, excluded_at, by) VALUES (?, ?, ?)",
                       (channel, now(), by))
            return True

    def remove_channel_exclusion(self, channel):
        """Undo: mark the active exclusion removed (rows are never deleted). False when none."""
        with self._transaction() as db:
            return db.execute("UPDATE channel_exclusions SET removed_at=? WHERE channel=? "
                              "AND removed_at IS NULL", (now(), channel)).rowcount > 0

    def list_channel_exclusions(self, *, include_removed=False):
        if not self.has_table("channel_exclusions"):
            return []
        query = "SELECT * FROM channel_exclusions"
        if not include_removed:
            query += " WHERE removed_at IS NULL"
        return self._rows(query + " ORDER BY channel, id")

    def channel_exclusion_ids(self):
        """Channels with an active ledger exclusion: the router reads them like excluded_channels."""
        return {row["channel"] for row in self.list_channel_exclusions()}

    def close_channel_items(self, channel, note, *, source="slack"):
        """Move every open item (new, assigned, acked, human_pending) of a channel to excluded."""
        with self._transaction() as db:
            return db.execute("UPDATE messages SET state='excluded', note=? WHERE source=? AND channel=? "
                              "AND state IN ('new', 'assigned', 'acked', 'human_pending')",
                              (note, source, channel)).rowcount

    # P16 user directory and bot flag repair.

    def get_user_directory(self, user):
        if not self.has_table("user_directory"):
            return None
        rows = self._rows("SELECT * FROM user_directory WHERE user=?", (user,))
        return rows[0] if rows else None

    def put_user_directory(self, row, seen_at):
        """Store one users.info answer (insert or replace by user id)."""
        with self._transaction() as db:
            db.execute("""INSERT INTO user_directory VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user) DO UPDATE SET is_bot=excluded.is_bot,
                is_app_user=excluded.is_app_user, deleted=excluded.deleted,
                name=excluded.name, seen_at=excluded.seen_at""",
                       (row["user"], int(bool(row.get("is_bot"))), int(bool(row.get("is_app_user"))),
                        int(bool(row.get("deleted"))), row.get("name"), seen_at))

    def list_user_directory(self):
        if not self.has_table("user_directory"):
            return []
        return self._rows("SELECT * FROM user_directory ORDER BY user")

    def bot_flagged_messages(self, *, since=None):
        """Rows flagged `is_bot = 1` that have an author (repair candidates), oldest first.
        `since` filters `received_at >= since` like `list_messages`. Bodies are omitted."""
        rows = self._rows("SELECT * FROM messages WHERE is_bot=1 AND author IS NOT NULL "
                          "AND (? IS NULL OR received_at >= ?) ORDER BY received_at, id",
                          (since, since))
        for row in rows:
            row.pop("body", None)
        return rows

    def set_is_bot(self, message_id, value):
        self._update(message_id, {"is_bot": int(bool(value))})


def event_is_bot(event):
    """The stored bot flag: the pipeline's `is_bot` when the event carries it, else the
    P16 rule without a directory or known bot users (missing author or `bot_message`)."""
    if event.get("is_bot") is not None:
        return bool(event["is_bot"])
    from slack_dispatch.ingest.user_directory import is_bot_message
    return is_bot_message(event, None)


def _numeric(ts):
    """Numeric Slack ts for ordering; unusable values sort first."""
    try:
        value = Decimal(str(ts))
        return value if value.is_finite() else Decimal("-Infinity")
    except (InvalidOperation, ValueError):
        return Decimal("-Infinity")
