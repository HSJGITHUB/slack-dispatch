"""Transactional SQLite ledger. Construction performs no I/O."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
import threading


@dataclass(frozen=True)
class RecordResult:
    message_id: str | None
    created: bool
    duplicate: bool
    kind: str
    # P22: for a message_changed event of an existing row, the stored body before this
    # update (None when no body was stored, store_body is off, or for other events).
    previous_body: str | None = None
    # P24: for a message_changed event of an existing row, its state and lane before this
    # event (an edit never changes either; the pipeline gates the edit on these).
    previous_state: str | None = None
    previous_lane: str | None = None


class ProposalStateError(Exception):
    """A scope proposal decision needs an open or suggested proposal (P13)."""


def now():
    return datetime.now().astimezone().isoformat()


# P18 delivery log. created_at is UTC ISO with microseconds, so text order is time order.
DELIVERY_LOG_DDL = """CREATE TABLE IF NOT EXISTS delivery_log (
    id INTEGER PRIMARY KEY, created_at TEXT, purpose TEXT, lane TEXT, kind TEXT, adapter TEXT,
    target_session_id TEXT, target_session_name TEXT, item_count INTEGER, item_ids TEXT,
    status TEXT, detail TEXT, cost_usd REAL, duration_ms INTEGER, relay_session_id TEXT,
    last_seen_at TEXT, repeat_count INTEGER)"""
DELIVERY_COLUMNS = ("created_at", "purpose", "lane", "kind", "adapter", "target_session_id",
                    "target_session_name", "item_count", "item_ids", "status", "detail",
                    "cost_usd", "duration_ms", "relay_session_id")
# fix1: a skipped attempt equal to the lane's previous row (same status and item ids) is
# folded into that row: last_seen_at moves and repeat_count grows (NULL counts as 1).
DELIVERY_REPEAT_COLUMNS = {"last_seen_at": "TEXT", "repeat_count": "INTEGER"}
DETAIL_CHARS = 500
# A done_at with one of these notes was written by the service, the operator or cutover,
# not by the lane session, so it is no delivery receipt (the inbox CLI SYSTEM_NOTES list).
RECEIPT_IGNORED_NOTES = ("cutover:", "owner_reaction:", "owner_post", "prehandled", "context",
                         "message_deleted", "human:")
CARD_DECISION_REASON = "human:card"
# P19 console undo journal: one row per console write that changed message rows. `before`
# and `after` are JSON lists of SNAPSHOT_FIELDS per message id; `meta` (additive) holds the
# wake_requests and channel_exclusions row ids the action wrote and the target lane.
CONSOLE_ACTIONS_DDL = """CREATE TABLE IF NOT EXISTS console_actions (
    id INTEGER PRIMARY KEY, at TEXT, action TEXT, ids TEXT, before TEXT, after TEXT, by TEXT,
    undo_until TEXT, undone_at TEXT, result TEXT, meta TEXT)"""
CONSOLE_ACTION_COLUMNS = {"at": "TEXT", "action": "TEXT", "ids": "TEXT", "before": "TEXT",
                          "after": "TEXT", "by": "TEXT", "undo_until": "TEXT",
                          "undone_at": "TEXT", "result": "TEXT", "meta": "TEXT"}
# P21 fix1: wake_requests.kind values.
WAKE_KIND_ASSIGN, WAKE_KIND_NOW = "assign", "wake_now"
SNAPSHOT_FIELDS = ("state", "lane", "note", "done_at", "ack_at", "assigned_at", "route_reason")
OPEN_STATES = ("new", "assigned", "acked", "human_pending")
# P20: when the delivery log started, stored once at migration as a `cursors` row (the
# ledger has no key-value table). Readers take the earlier of this and the first row.
META_SOURCE = "meta"
DELIVERY_LOG_STARTED = "delivery_log_started_at"


class UndoStateError(Exception):
    """A console action can no longer be undone: `undone` or `expired` (P19)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def utc_stamp(epoch):
    """UTC ISO time with microseconds for delivery_log.created_at (sortable as text)."""
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat(timespec="microseconds")


def _moment(value):
    """Aware datetime from an ISO stamp (naive values are local time), or None."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else moment.astimezone()


def delivery_item_ids(value):
    """The message ids of a delivery_log `item_ids` value (JSON list text or a list)."""
    try:
        ids = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return []
    return [item for item in ids if isinstance(item, str)] if isinstance(ids, list) else []


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
            # P21 fix1: what queued the request, `assign` (P12 assign, reroute, card) or
            # `wake_now` (console Wake now, CLI wake), which may also wake an acked item.
            present = {r[1] for r in db.execute("PRAGMA table_info(wake_requests)")}
            if "kind" not in present:
                db.execute("ALTER TABLE wake_requests ADD COLUMN kind TEXT NOT NULL "
                           f"DEFAULT '{WAKE_KIND_ASSIGN}'")
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
            # P18: one row per wake or human notice attempt (additive).
            db.execute(DELIVERY_LOG_DDL)
            present = {r[1] for r in db.execute("PRAGMA table_info(delivery_log)")}
            for name, declaration in DELIVERY_REPEAT_COLUMNS.items():
                if name not in present:
                    db.execute(f"ALTER TABLE delivery_log ADD COLUMN {name} {declaration}")
            db.execute("CREATE INDEX IF NOT EXISTS delivery_log_created ON delivery_log (created_at)")
            # P20: the delivery log start, written once (the first row's time when rows exist).
            first = db.execute("SELECT MIN(created_at) FROM delivery_log").fetchone()[0]
            db.execute("INSERT OR IGNORE INTO cursors (source, channel, cursor) VALUES (?, ?, ?)",
                       (META_SOURCE, DELIVERY_LOG_STARTED,
                        first or datetime.now(timezone.utc).isoformat(timespec="microseconds")))
            # P19: console action journal for undo (additive).
            db.execute(CONSOLE_ACTIONS_DDL)
            present = {r[1] for r in db.execute("PRAGMA table_info(console_actions)")}
            for name, declaration in CONSOLE_ACTION_COLUMNS.items():
                if name not in present:
                    db.execute(f"ALTER TABLE console_actions ADD COLUMN {name} {declaration}")
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
        previous_body = previous_state = previous_lane = None
        with self._transaction() as db:
            if event_id:
                seen = db.execute("SELECT * FROM events_seen WHERE event_id=?", (event_id,)).fetchone()
                if seen:
                    return RecordResult(seen["message_id"], False, True, seen["kind"])
            if kind == "message" and mid:
                exists = db.execute("SELECT 1 FROM messages WHERE id=?", (mid,)).fetchone()
                subtype = event.get("subtype")
                if subtype == "message_changed":
                    if exists:
                        # P22: the body before the edit, so the pipeline can tell a text
                        # change from a metadata-only edit (link previews attaching).
                        before = db.execute("SELECT body, state, lane FROM messages WHERE id=?",
                                            (mid,)).fetchone()
                        previous_body = before["body"]
                        previous_state, previous_lane = before["state"], before["lane"]
                    # P24: only edit metadata and the body change here; state, lane and
                    # route_reason are never touched by an edit.
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
        return RecordResult(mid, created, duplicate, kind, previous_body, previous_state,
                            previous_lane)

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

    def request_wake(self, lane, message_id, source, kind=None):
        """Queue one lane wake for a delivered message; the service consumes it.

        P21 fix1: `kind` is `assign` (default) or `wake_now`."""
        with self._transaction() as db:
            return db.execute("INSERT INTO wake_requests (lane, message_id, requested_at, source, kind) "
                              "VALUES (?, ?, ?, ?, ?)",
                              (lane, message_id, now(), source, kind or WAKE_KIND_ASSIGN)).lastrowid

    def pending_wake_ids(self, lane):
        """Message ids of the lane with an unconsumed wake request (P21 fix1)."""
        return {row["message_id"] for row in self._rows(
            "SELECT DISTINCT message_id FROM wake_requests WHERE lane=? AND consumed_at IS NULL",
            (lane,))}

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

    # P17 owner resolver reads (additive queries only).

    def channel_lane_activity(self, channel, *, source="slack"):
        """Lane, state, ack_at and done_at of the channel's lane-owned items that a lane acked
        or completed (states acked and done). No bodies; the resolver filters by time."""
        return self._rows("SELECT id, lane, state, ack_at, done_at FROM messages WHERE source=? "
                          "AND channel=? AND lane IS NOT NULL AND state IN ('acked', 'done') "
                          "ORDER BY id", (source, channel))

    def pending_in_thread(self, source, channel, root):
        """`human_pending` rows of a thread (the root itself included), oldest first. No bodies."""
        rows = self._rows("SELECT * FROM messages WHERE source=? AND channel=? AND (ts=? OR thread_ts=?) "
                          "AND state='human_pending'", (source, channel, root, root))
        for row in rows:
            row.pop("body", None)
        return sorted(rows, key=lambda row: (_numeric(row["ts"]), row["id"]))

    def owner_reply_items(self, source, channel, root, direct_lane=None):
        """P27: rows of a thread (the root included) an owner reply may close, oldest first:
        `human_pending` rows, plus `assigned` and `acked` rows of `direct_lane` when one is
        given. Rows of other lanes are never returned. No bodies; the caller applies the ts
        rule."""
        rows = self._rows("SELECT * FROM messages WHERE source=? AND channel=? AND (ts=? OR thread_ts=?) "
                          "AND (state='human_pending' OR (lane=? AND state IN ('assigned', 'acked')))",
                          (source, channel, root, root, direct_lane))
        for row in rows:
            row.pop("body", None)
        return sorted(rows, key=lambda row: (_numeric(row["ts"]), row["id"]))

    def open_messages(self, *, lane=None):
        """Open items (human_pending, assigned, acked), optionally of one lane. No bodies."""
        rows = self._rows("SELECT * FROM messages WHERE state IN ('human_pending', 'assigned', 'acked') "
                          "AND (? IS NULL OR lane=?) ORDER BY received_at, id", (lane, lane))
        for row in rows:
            row.pop("body", None)
        return rows

    # P18 delivery log: one row per wake or human notice attempt.

    def add_delivery(self, row):
        """Insert one delivery_log row and return its id.

        `item_ids` may be a list (stored as JSON); `detail` is cut to 500 characters;
        `created_at` defaults to now (UTC). Unknown keys are ignored. A `skipped_*` row whose
        status and item ids equal the previous row of the same lane and purpose is not
        inserted: that row gets `last_seen_at` = this created_at and `repeat_count` + 1, and
        its id is returned.
        """
        values = {key: row.get(key) for key in DELIVERY_COLUMNS}
        if values["created_at"] is None:
            values["created_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        ids = values["item_ids"]
        if not isinstance(ids, str):
            ids = json.dumps([str(item) for item in (ids or [])])
            values["item_ids"] = ids
        if values["item_count"] is None:
            values["item_count"] = len(delivery_item_ids(ids))
        if isinstance(values["detail"], str):
            values["detail"] = values["detail"][:DETAIL_CHARS]
        columns = ", ".join(DELIVERY_COLUMNS)
        marks = ", ".join("?" for _ in DELIVERY_COLUMNS)
        with self._transaction() as db:
            if str(values["status"] or "").startswith("skipped_"):
                previous = db.execute("SELECT id, status, item_ids FROM delivery_log "
                                      "WHERE lane IS ? AND purpose IS ? ORDER BY id DESC LIMIT 1",
                                      (values["lane"], values["purpose"])).fetchone()
                if (previous is not None and previous["status"] == values["status"]
                        and delivery_item_ids(previous["item_ids"]) == delivery_item_ids(ids)):
                    db.execute("UPDATE delivery_log SET last_seen_at=?, "
                               "repeat_count=COALESCE(repeat_count, 1) + 1 WHERE id=?",
                               (values["created_at"], previous["id"]))
                    return previous["id"]
            return db.execute(f"INSERT INTO delivery_log ({columns}) VALUES ({marks})",
                              tuple(values[key] for key in DELIVERY_COLUMNS)).lastrowid

    @staticmethod
    def _delivery_filter(since, lane, purpose, status):
        clauses, params = [], []
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        for column, value in (("lane", lane), ("purpose", purpose)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if status == "skipped":
            clauses.append("substr(status, 1, 8) = 'skipped_'")
        elif status is not None:
            clauses.append("status = ?")
            params.append(status)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def list_deliveries(self, *, since=None, lane=None, purpose=None, status=None, limit=None,
                        offset=0):
        """delivery_log rows newest first. `since` is a UTC ISO bound (created_at >= since);
        status `skipped` matches every skipped_* status. Empty without the table."""
        if not self.has_table("delivery_log"):
            return []
        where, params = self._delivery_filter(since, lane, purpose, status)
        limit = -1 if limit is None else limit
        return self._rows(f"SELECT * FROM delivery_log{where} ORDER BY created_at DESC, id DESC "
                          "LIMIT ? OFFSET ?", (*params, limit, offset))

    def count_deliveries(self, *, since=None, lane=None, purpose=None, status=None):
        if not self.has_table("delivery_log"):
            return 0
        where, params = self._delivery_filter(since, lane, purpose, status)
        return self._rows(f"SELECT COUNT(*) AS n FROM delivery_log{where}", params)[0]["n"]

    def prune_deliveries(self, before):
        """Delete delivery_log rows created before the UTC ISO bound; returns the count."""
        if not self.has_table("delivery_log"):
            return 0
        with self._transaction() as db:
            return db.execute("DELETE FROM delivery_log WHERE created_at < ?", (before,)).rowcount

    def delivery_receipts(self, rows, now=None):
        """Receipt fields for delivery_log rows, in order (new dicts; rows are unchanged).

        A `lane` row with status `sent` is received at the earliest `ack_at` or `done_at` of
        its items that is later than the row's created_at, on items whose lane equals the
        row lane (a later reroute hides the receipt). A done_at written with a service,
        operator or cutover note does not count. `latency_seconds` is the delay; without a receipt
        `received_at` is None and `age_seconds` is the row age at `now` (epoch seconds,
        default the current time). `human_queue` rows have no receipt; a `card` row has
        `decision_at` when a card decision assigned an item after the row (route reason
        `human:card`), else None. Rows that were not sent (skipped, failed, dry run) get
        no receipt and no age.
        """
        now = datetime.now(timezone.utc).timestamp() if now is None else float(now)
        ids = sorted({mid for row in rows for mid in delivery_item_ids(row.get("item_ids"))})
        messages = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ", ".join("?" for _ in chunk)
            for message in self._rows(
                    "SELECT id, lane, state, ack_at, done_at, note, route_reason, assigned_at "
                    f"FROM messages WHERE id IN ({marks})", chunk):
                messages[message["id"]] = message
        result = []
        for row in rows:
            created = _moment(row.get("created_at"))
            receipt = {"received_at": None, "latency_seconds": None, "age_seconds": None,
                       "decision_at": None}
            items = delivery_item_ids(row.get("item_ids"))
            if row.get("purpose") == "lane" and row.get("status") == "sent" and created is not None:
                best = None
                for mid in items:
                    message = messages.get(mid)
                    if not message or message.get("lane") != row.get("lane"):
                        continue
                    stamps = [message.get("ack_at")]
                    if not str(message.get("note") or "").startswith(RECEIPT_IGNORED_NOTES):
                        stamps.append(message.get("done_at"))
                    for stamp in stamps:
                        moment = _moment(stamp)
                        if moment is not None and moment > created and (
                                best is None or moment < best[0]):
                            best = (moment, stamp)
                if best is not None:
                    receipt["received_at"] = best[1]
                    receipt["latency_seconds"] = int((best[0] - created).total_seconds())
                else:
                    receipt["age_seconds"] = max(0, int(now - created.timestamp()))
            elif row.get("kind") == "card" and created is not None:
                best = None
                for mid in items:
                    message = messages.get(mid) or {}
                    moment = _moment(message.get("assigned_at"))
                    if (message.get("route_reason") == CARD_DECISION_REASON and moment is not None
                            and moment > created and (best is None or moment < best[0])):
                        best = (moment, message["assigned_at"])
                receipt["decision_at"] = best[1] if best else None
            result.append(dict(row, **receipt))
        return result

    # P19 console action journal and undo.

    def message_snapshots(self, ids):
        """{id: {id, SNAPSHOT_FIELDS...}} for the ids present in the ledger (no bodies)."""
        ids = list(dict.fromkeys(ids))
        result = {}
        columns = ", ".join(("id",) + SNAPSHOT_FIELDS)
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ", ".join("?" for _ in chunk)
            for row in self._rows(f"SELECT {columns} FROM messages WHERE id IN ({marks})", chunk):
                result[row["id"]] = row
        return result

    def binding_snapshots(self, ids):
        """{id: {source, channel, thread_ts, lane}}: the thread binding each message's thread
        (thread_ts, else ts) has now, lane None without a binding (fix1, for undo)."""
        ids = list(dict.fromkeys(ids))
        result = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ", ".join("?" for _ in chunk)
            for row in self._rows(
                    "SELECT m.id, COALESCE(m.source, 'slack') AS source, m.channel, "
                    "COALESCE(m.thread_ts, m.ts) AS root, b.lane FROM messages m "
                    "LEFT JOIN bindings b ON b.source=COALESCE(m.source, 'slack') "
                    "AND b.channel=m.channel AND b.thread_ts=COALESCE(m.thread_ts, m.ts) "
                    f"WHERE m.id IN ({marks})", chunk):
                if row["channel"] and row["root"]:
                    result[row["id"]] = {"source": row["source"], "channel": row["channel"],
                                         "thread_ts": row["root"], "lane": row["lane"]}
        return result

    def open_channel_ids(self, channel, *, source="slack"):
        """Ids of the channel's open items (the rows `close_channel_items` would close)."""
        marks = ", ".join("?" for _ in OPEN_STATES)
        return [row["id"] for row in self._rows(
            f"SELECT id FROM messages WHERE source=? AND channel=? AND state IN ({marks}) ORDER BY id",
            (source, channel, *OPEN_STATES))]

    def wake_request_mark(self):
        """The highest wake_requests id (0 when empty): rows above it are newer."""
        return self._rows("SELECT COALESCE(MAX(id), 0) AS n FROM wake_requests")[0]["n"]

    def channel_exclusion_mark(self):
        """The highest channel_exclusions id (0 when empty)."""
        return self._rows("SELECT COALESCE(MAX(id), 0) AS n FROM channel_exclusions")[0]["n"]

    def wake_requests_after(self, mark, ids, source):
        """wake_requests rows above `mark` for the message ids and source, oldest first."""
        wanted = set(ids)
        return [row for row in self._rows("SELECT * FROM wake_requests WHERE id>? AND source=? "
                                          "ORDER BY id", (mark, source))
                if row["message_id"] in wanted]

    def channel_exclusions_after(self, mark, by):
        """channel_exclusions rows above `mark` written by `by`, oldest first."""
        return self._rows("SELECT * FROM channel_exclusions WHERE id>? AND by=? ORDER BY id",
                          (mark, by))

    def add_console_action(self, *, at, action, ids, before, after, by, undo_until, meta=None):
        """Insert one journal row (lists and dicts are stored as JSON) and return its id."""
        with self._transaction() as db:
            return db.execute(
                "INSERT INTO console_actions (at, action, ids, before, after, by, undo_until, meta) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (at, action, json.dumps(list(ids)), json.dumps(before), json.dumps(after), by,
                 undo_until, json.dumps(meta or {}))).lastrowid

    def get_console_action(self, action_id):
        rows = self._rows("SELECT * FROM console_actions WHERE id=?", (action_id,))
        return rows[0] if rows else None

    def list_console_actions(self, limit=20, *, pending_only=False):
        """Journal rows newest first; `pending_only` keeps rows not undone yet."""
        if not self.has_table("console_actions"):
            return []
        where = " WHERE undone_at IS NULL" if pending_only else ""
        return self._rows(f"SELECT * FROM console_actions{where} ORDER BY id DESC LIMIT ?", (limit,))

    def undo_console_action(self, action_id, *, at, now_epoch, by="console"):
        """Restore the `before` fields of one journal row in one transaction.

        Allowed while `now_epoch` is at or before `undo_until` and the row is not undone
        (else UndoStateError `expired` or `undone`; KeyError for an unknown id). A message is
        restored only when its state and lane still equal the `after` snapshot, else it is
        reported as `conflict` and left unchanged. For a restored message the action's
        unconsumed wake_requests rows are removed; a consumed one sets `wake_sent`.

        fix1: a thread binding the action changed (entry key `binding`) goes back to its
        earlier lane (or is removed when there was none) when at least one restored message
        carries it and the binding still has the action's lane; otherwise those entries get
        `binding: binding_conflict` and the binding is left alone. When no message was
        restored (`nothing_restored`), the channel exclusions are kept; otherwise the
        exclusions the action wrote are ended (removed_at) when still active.
        Returns the result, which is also stored on the row with `undone_at`.
        """
        with self._transaction() as db:
            row = db.execute("SELECT * FROM console_actions WHERE id=?", (action_id,)).fetchone()
            if row is None:
                raise KeyError(action_id)
            if row["undone_at"]:
                raise UndoStateError("undone")
            until = _moment(row["undo_until"])
            if until is None or float(now_epoch) > until.timestamp():
                raise UndoStateError("expired")
            before = {entry["id"]: entry for entry in _json_list(row["before"])}
            after = {entry["id"]: entry for entry in _json_list(row["after"])}
            meta = _json_dict(row["meta"])
            wakes = {}
            for wake_id in meta.get("wake_requests") or []:
                wake = db.execute("SELECT * FROM wake_requests WHERE id=?", (wake_id,)).fetchone()
                if wake is not None:
                    wakes.setdefault(wake["message_id"], []).append(dict(wake))
            assignments = ", ".join(f"{name}=?" for name in SNAPSHOT_FIELDS)
            results = []
            threads = {}  # binding key -> (earlier binding, binding after, restored entries)
            for mid in _json_list(row["ids"]):
                entry = {"id": mid, "status": "restored", "detail": None, "wake_sent": False,
                         "wake_removed": 0, "binding": None}
                results.append(entry)
                current = db.execute("SELECT state, lane FROM messages WHERE id=?", (mid,)).fetchone()
                old, new = before.get(mid), after.get(mid)
                if current is None or old is None or new is None:
                    entry["status"] = "not_found"
                    continue
                if (current["state"], current["lane"]) != (new.get("state"), new.get("lane")):
                    entry.update(status="conflict", detail="now " + str(current["state"]) + " in "
                                 + str(current["lane"] or "no lane"))
                    continue
                db.execute(f"UPDATE messages SET {assignments} WHERE id=?",
                           (*(old.get(name) for name in SNAPSHOT_FIELDS), mid))
                if isinstance(old.get("binding"), dict) and isinstance(new.get("binding"), dict):
                    key = tuple(old["binding"].get(name) for name in ("source", "channel", "thread_ts"))
                    first = threads.setdefault(key, (old["binding"], new["binding"], []))
                    first[2].append(entry)
                for wake in wakes.get(mid, []):
                    if wake["consumed_at"]:
                        entry["wake_sent"] = True
                    else:
                        db.execute("DELETE FROM wake_requests WHERE id=? AND consumed_at IS NULL",
                                   (wake["id"],))
                        entry["wake_removed"] += 1
            for (source, channel, thread_ts), (earlier, later, entries) in threads.items():
                current = db.execute("SELECT lane FROM bindings WHERE source=? AND channel=? "
                                     "AND thread_ts=?", (source, channel, thread_ts)).fetchone()
                if (current["lane"] if current else None) != later.get("lane"):
                    status = "binding_conflict"
                elif earlier.get("lane") is None:
                    db.execute("DELETE FROM bindings WHERE source=? AND channel=? AND thread_ts=?",
                               (source, channel, thread_ts))
                    status = "restored"
                else:
                    db.execute("""INSERT INTO bindings VALUES (?, ?, ?, ?)
                        ON CONFLICT(source, channel, thread_ts) DO UPDATE SET lane=excluded.lane""",
                               (source, channel, thread_ts, earlier["lane"]))
                    status = "restored"
                for entry in entries:
                    entry["binding"] = status
            nothing_restored = not any(entry["status"] == "restored" for entry in results)
            ended = 0
            if not nothing_restored:
                for exclusion_id in meta.get("channel_exclusions") or []:
                    ended += db.execute("UPDATE channel_exclusions SET removed_at=? WHERE id=? "
                                        "AND removed_at IS NULL", (at, exclusion_id)).rowcount
            counts = {name: sum(entry["status"] == name for entry in results)
                      for name in ("restored", "conflict", "not_found")}
            counts["wake_sent"] = sum(entry["wake_sent"] for entry in results)
            counts["wake_removed"] = sum(entry["wake_removed"] for entry in results)
            counts["channel_exclusions_ended"] = ended
            counts["binding_restored"] = sum(entry["binding"] == "restored" for entry in results)
            counts["binding_conflict"] = sum(entry["binding"] == "binding_conflict"
                                             for entry in results)
            result = {"action_id": row["id"], "action": row["action"], "undone_at": at, "by": by,
                      "nothing_restored": nothing_restored, "results": results, "counts": counts}
            db.execute("UPDATE console_actions SET undone_at=?, result=? WHERE id=?",
                       (at, json.dumps(result), row["id"]))
            return result

    # P20 display names, permalinks and the Deliveries empty state (additive reads only).

    def distinct_authors(self, *, source="slack"):
        """Every distinct non-empty author id of the ledger's messages, sorted."""
        return [row["author"] for row in self._rows(
            "SELECT DISTINCT author FROM messages WHERE source=? AND author IS NOT NULL "
            "AND author != '' ORDER BY author", (source,))]

    def authors_without_directory(self, *, source="slack"):
        """Distinct authors that have no `user_directory` row, sorted."""
        if not self.has_table("user_directory"):
            return self.distinct_authors(source=source)
        return [row["author"] for row in self._rows(
            "SELECT DISTINCT m.author FROM messages m LEFT JOIN user_directory d "
            "ON d.user = m.author WHERE m.source=? AND m.author IS NOT NULL AND m.author != '' "
            "AND d.user IS NULL ORDER BY m.author", (source,))]

    def message_refs(self, ids):
        """{id: {channel, ts, thread_ts}} for the ids present in the ledger (no bodies)."""
        ids = sorted({str(item) for item in ids or ()})
        refs = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ", ".join("?" for _ in chunk)
            for row in self._rows(f"SELECT id, channel, ts, thread_ts FROM messages WHERE id IN ({marks})",
                                  chunk):
                refs[row["id"]] = {"channel": row["channel"], "ts": row["ts"],
                                   "thread_ts": row["thread_ts"]}
        return refs

    def delivery_log_started_at(self):
        """When the delivery log started (UTC ISO): the earlier of the time stored at
        migration and the first `delivery_log` row; None when neither exists."""
        stamps = []
        stored = self.get_cursor(META_SOURCE, DELIVERY_LOG_STARTED)
        if stored:
            stamps.append(stored)
        if self.has_table("delivery_log"):
            first = self._rows("SELECT MIN(created_at) AS at FROM delivery_log")[0]["at"]
            if first:
                stamps.append(first)
        moments = [(moment, stamp) for stamp in stamps
                   for moment in [_moment(stamp)] if moment is not None]
        return min(moments)[1] if moments else None

    def count_service_deliveries(self, *, since=None, before=None, lane=None):
        """Rows of the service-owned `service_deliveries` table (per thread and lane, latest
        delivery time in epoch seconds) with since <= delivered_at < before; 0 without it."""
        if not self.has_table("service_deliveries"):
            return 0
        return self._rows("SELECT COUNT(*) AS n FROM service_deliveries WHERE "
                          "(? IS NULL OR delivered_at >= ?) AND (? IS NULL OR delivered_at < ?) "
                          "AND (? IS NULL OR lane = ?)",
                          (since, since, before, before, lane, lane))[0]["n"]


def _json_list(value):
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _json_dict(value):
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


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
