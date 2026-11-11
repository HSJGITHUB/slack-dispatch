-- Draft only. The store does not execute migrations yet.
PRAGMA foreign_keys = ON;
CREATE TABLE lanes (
    id TEXT PRIMARY KEY,
    session_pointer TEXT,
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1))
);
CREATE TABLE messages (
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
    UNIQUE (source, channel, ts)
);
CREATE TABLE bindings (
    source TEXT NOT NULL,
    channel TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    lane TEXT NOT NULL REFERENCES lanes(id),
    PRIMARY KEY (source, channel, thread_ts)
);
-- Optional, disabled by default. Retention and access policy must precede enablement.
CREATE TABLE events_raw (
    event_id TEXT PRIMARY KEY,
    message_id TEXT REFERENCES messages(id),
    received_at TEXT NOT NULL,
    payload TEXT
);
CREATE TABLE cursors (
    source TEXT NOT NULL,
    channel TEXT NOT NULL,
    cursor TEXT NOT NULL,
    PRIMARY KEY (source, channel)
);
