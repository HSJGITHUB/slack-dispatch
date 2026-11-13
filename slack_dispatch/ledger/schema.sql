-- Version 1. Body storage is optional and disabled by default.
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS lanes (
    id TEXT PRIMARY KEY,
    session_pointer TEXT,
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1))
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    channel TEXT NOT NULL,
    ts TEXT NOT NULL,
    thread_ts TEXT,
    author TEXT,
    is_thread_reply INTEGER NOT NULL CHECK (is_thread_reply IN (0, 1)),
    received_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('new', 'assigned', 'acked', 'done', 'human_pending', 'excluded')),
    lane TEXT REFERENCES lanes(id),
    route_reason TEXT,
    classifier_suggestion TEXT,
    classifier_version TEXT,
    followup INTEGER NOT NULL DEFAULT 0 CHECK (followup IN (0, 1)),
    ack_at TEXT,
    done_at TEXT,
    assigned_at TEXT,
    body TEXT, -- optional; off by default. Wake/notify payloads carry ids only, never body.
    event_id TEXT,
    channel_type TEXT,
    has_files INTEGER NOT NULL DEFAULT 0,
    edited_at TEXT,
    note TEXT,
    suggestion_reason TEXT,
    attention TEXT,
    is_bot INTEGER NOT NULL DEFAULT 0,
    origin TEXT,
    UNIQUE (source, channel, ts)
);
CREATE TABLE IF NOT EXISTS bindings (
    source TEXT NOT NULL,
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    lane TEXT NOT NULL REFERENCES lanes(id),
    PRIMARY KEY (source, channel, thread_ts)
);
-- Optional, disabled by default. Retention and access policy must precede enablement.
CREATE TABLE IF NOT EXISTS events_raw (
    event_id TEXT PRIMARY KEY,
    message_id TEXT REFERENCES messages(id),
    received_at TEXT NOT NULL,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS cursors (
    source TEXT NOT NULL,
    channel TEXT NOT NULL,
    cursor TEXT NOT NULL,
    PRIMARY KEY (source, channel)
);

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS events_seen (
    event_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    message_id TEXT,
    source TEXT,
    channel TEXT
);
CREATE TABLE IF NOT EXISTS reactions (
    message_id TEXT NOT NULL,
    user TEXT NOT NULL,
    reaction TEXT NOT NULL,
    added_at TEXT,
    removed_at TEXT,
    PRIMARY KEY (message_id, user, reaction)
);

CREATE TABLE IF NOT EXISTS human_notices (
    message_id TEXT NOT NULL REFERENCES messages(id),
    kind TEXT NOT NULL,
    adapter INTEGER NOT NULL,
    queued_at REAL NOT NULL,
    immediate INTEGER NOT NULL DEFAULT 0,
    sent_at REAL,
    PRIMARY KEY (message_id, kind, adapter)
);
CREATE TABLE IF NOT EXISTS timer_events (
    message_id TEXT NOT NULL REFERENCES messages(id),
    timer TEXT NOT NULL,
    adapter INTEGER NOT NULL,
    sent_at REAL NOT NULL,
    PRIMARY KEY (message_id, timer, adapter)
);

CREATE TABLE IF NOT EXISTS ingest_gaps (
    id INTEGER PRIMARY KEY,
    source TEXT,
    detected_at TEXT,
    last_event_at TEXT,
    resumed_at TEXT,
    catchup_at TEXT,
    note TEXT
);

-- P13: lane scope claims, learned scope proposals and recorded sends (additive).
-- Claims with status `confirmed` and no removed_at are the ledger-backed routing overrides.
CREATE TABLE IF NOT EXISTS scope_claims (
    id INTEGER PRIMARY KEY,
    lane TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('channel', 'dm', 'person', 'thread')),
    key TEXT NOT NULL,
    note TEXT,
    claimed_at TEXT NOT NULL,
    claimed_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'claimed' CHECK (status IN ('claimed', 'confirmed')),
    confirmed_at TEXT,
    confirmed_by TEXT,
    removed_at TEXT
);
CREATE INDEX IF NOT EXISTS scope_claims_key ON scope_claims (kind, key);
CREATE TABLE IF NOT EXISTS scope_proposals (
    kind TEXT NOT NULL CHECK (kind IN ('channel', 'dm', 'person')),
    key TEXT NOT NULL,
    lane TEXT NOT NULL,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'suggested', 'confirmed', 'rejected', 'deferred', 'superseded')),
    conflict INTEGER NOT NULL DEFAULT 0,
    last_evidence TEXT,
    imported INTEGER NOT NULL DEFAULT 0,
    decided_at TEXT,
    PRIMARY KEY (kind, key, lane)
);
CREATE TABLE IF NOT EXISTS sends (
    channel TEXT NOT NULL,
    ts TEXT NOT NULL,
    thread_ts TEXT,
    lane TEXT NOT NULL,
    kind TEXT,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (channel, ts)
);
