"""Read-only index of a session thread registry JSON file (P17).

The registry is a hand-maintained JSON file that Claude sessions use to record which
Slack threads they own. It is large and irregular, so this reader normalizes it:

- channel under `ch`, `channel_id` or `channel`; the value may be an id followed by a
  name (`C0DUMMY0001 #name`) or a name only; the first token that starts with a
  conversation id (`^[CDG][A-Z0-9]{8,}`) is the channel, else the row has no channel;
- root ts under `root_ts` or `thread_ts`; `-`, a missing value or a value that is not a
  Slack ts means a flat channel or DM row without a root;
- duplicate (channel, root) pairs keep every row; the effective row (status, badge) is
  the one with the newest `last_ts`, then the one later in the file;
- free text fields are kept as text and never interpreted beyond the derived fields.

Derived per row: `session_ids` (every 8 hex token in `owner`, `owner_session`,
`session`, lower case), `closed` (status in the configured closed statuses) and
`last_ts` (the largest Slack ts among `last_out_ts`, `last_in_ts`, `last_event`).

`_meta` entries whose key starts with `sweep_scope` and that hold `channels` or `dms`
lists are declared sweep scopes; `routing_rules` entries (`trigger`/`route` or
`if`/`then`) keep their raw text plus the channel and session ids found in it.

`RegistryIndex.load(path)` parses a file once per (mtime, size) and caches the index.
A failed parse keeps the previous index and logs one WARNING with the exception class
only. Nothing here calls Slack, a model or the network.
"""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json
import logging
from pathlib import Path
import re
import threading

LOG = logging.getLogger(__name__)

DEFAULT_CLOSED_STATUSES = ("closed", "answered", "answered_action_ours",
                           "sent_closed_no_action_ours", "delivered")
CONVERSATION_PREFIX = re.compile(r"^[CDG][A-Z0-9]{8,}")
CONVERSATION_ANY = re.compile(r"(?<![A-Za-z0-9])[CDG][A-Z0-9]{8,}(?![a-z0-9])")
SESSION_ID = re.compile(r"(?<![0-9a-f])[0-9a-f]{8}(?![0-9a-f])")
SLACK_TS = re.compile(r"^\d+\.\d+$")
TOKEN_SPLIT = re.compile(r"[\s,;()\[\]{}<>|/·]+")
CHANNEL_FIELDS = ("ch", "channel_id", "channel")
ROOT_FIELDS = ("root_ts", "thread_ts")
OWNER_FIELDS = ("owner", "owner_session", "session")
TEXT_FIELDS = ("id", "title", "status", "owner", "owner_session", "session", "routing",
               "waiting_on", "next_action", "last_out_ts", "last_in_ts", "last_event")
LAST_TS_FIELDS = ("last_out_ts", "last_in_ts", "last_event")
DISPLAY_CHARS = 160


def text(value):
    """A registry field as one stripped line of text, or None for empty or non-scalar."""
    if value is None or isinstance(value, (dict, list)):
        return None
    value = " ".join(str(value).split())
    return value or None


def clip(value, limit=DISPLAY_CHARS):
    """A display text of at most `limit` characters (an ellipsis marks a cut), or None."""
    if value is None:
        return None
    return value if len(value) <= limit else value[:limit - 3].rstrip() + "..."


def channel_id(value):
    """The first token that starts with a conversation id, else None."""
    value = text(value)
    if not value:
        return None
    for token in TOKEN_SPLIT.split(value):
        match = CONVERSATION_PREFIX.match(token)
        if match:
            return match.group(0)
    return None


def slack_ts(value):
    """`digits.digits` as a string, else None (`-`, missing, dates and free text)."""
    value = text(value)
    return value if value and SLACK_TS.match(value) else None


def ts_value(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def session_ids(*values):
    """Every 8 hex token in the values, lower case, in order of first appearance."""
    found = []
    for value in values:
        value = text(value)
        if value:
            found.extend(SESSION_ID.findall(value.lower()))
    return list(dict.fromkeys(found))


def conversation_ids(value):
    value = text(value)
    return list(dict.fromkeys(CONVERSATION_ANY.findall(value))) if value else []


@dataclass(frozen=True)
class RegistryRow:
    index: int
    id: str | None
    channel: str | None
    channel_raw: str | None
    root: str | None
    status: str | None
    title: str | None
    owner: str | None
    owner_session: str | None
    session: str | None
    routing: str | None
    waiting_on: str | None
    next_action: str | None
    last_out_ts: str | None
    last_in_ts: str | None
    last_event: str | None
    session_ids: tuple[str, ...]
    closed: bool
    last_ts: str | None

    def owner_text(self):
        """Owner text for display: owner, else owner_session, else session."""
        return self.owner or self.owner_session or self.session

    def summary(self):
        """The fields the console and CLI show (no free-form notes); free text is clipped
        to 160 characters."""
        return {"id": self.id, "title": clip(self.title), "status": self.status,
                "owner": clip(self.owner_text()), "waiting_on": clip(self.waiting_on),
                "next_action": clip(self.next_action), "closed": self.closed,
                "last_ts": self.last_ts}

    def close_note(self):
        """Done note `registry:<status>:<registry id>`; a row without an id uses
        `registry:<status>:<channel>:<root_ts>`. Missing parts are `-`, never None."""
        key = self.id or f"{self.channel or '-'}:{self.root or '-'}"
        return f"registry:{self.status or '-'}:{key}"


@dataclass(frozen=True)
class SweepScope:
    key: str
    kind: str  # channel or dm
    conversation: str
    session: str | None


@dataclass(frozen=True)
class RoutingRule:
    index: int
    text: str
    channels: tuple[str, ...]
    sessions: tuple[str, ...]


@dataclass
class RegistryIndex:
    """Normalized rows of one registry file. Empty when built without a file."""
    path: str | None = None
    rows: list = field(default_factory=list)
    by_key: dict = field(default_factory=dict)
    by_channel: dict = field(default_factory=dict)
    sweep: dict = field(default_factory=dict)
    rules: list = field(default_factory=list)
    stamp: tuple | None = None

    # ---------- parsing ----------

    @classmethod
    def parse(cls, data, *, path=None, closed_statuses=DEFAULT_CLOSED_STATUSES, stamp=None):
        """Build an index from decoded registry JSON (a mapping)."""
        if not isinstance(data, dict):
            raise ValueError("Registry must be a JSON object")
        closed = {status.strip().lower() for status in closed_statuses}
        index = cls(path=path, stamp=stamp)
        threads = data.get("threads")
        for position, raw in enumerate(threads if isinstance(threads, list) else []):
            if isinstance(raw, dict):
                index._add_row(position, raw, closed)
        meta = data.get("_meta")
        if isinstance(meta, dict):
            for key, entry in meta.items():
                if str(key).startswith("sweep_scope") and isinstance(entry, dict):
                    index._add_sweep(str(key), entry)
        rules = data.get("routing_rules")
        for position, rule in enumerate(rules if isinstance(rules, list) else []):
            if isinstance(rule, dict):
                index._add_rule(position, rule)
        return index

    def _add_row(self, position, raw, closed):
        values = {name: text(raw.get(name)) for name in TEXT_FIELDS}
        channel_raw = next((text(raw.get(name)) for name in CHANNEL_FIELDS
                            if text(raw.get(name))), None)
        channel = channel_id(channel_raw)
        root = next((slack_ts(raw.get(name)) for name in ROOT_FIELDS
                     if text(raw.get(name))), None)
        stamps = [value for value in (slack_ts(raw.get(name)) for name in LAST_TS_FIELDS) if value]
        last_ts = max(stamps, key=ts_value) if stamps else None
        status = values["status"]
        row = RegistryRow(
            index=position, id=values["id"], channel=channel, channel_raw=channel_raw,
            root=root, status=status, title=values["title"], owner=values["owner"],
            owner_session=values["owner_session"], session=values["session"],
            routing=values["routing"], waiting_on=values["waiting_on"],
            next_action=values["next_action"], last_out_ts=values["last_out_ts"],
            last_in_ts=values["last_in_ts"], last_event=values["last_event"],
            session_ids=tuple(session_ids(*(raw.get(name) for name in OWNER_FIELDS))),
            closed=bool(status) and status.strip().lower() in closed, last_ts=last_ts)
        self.rows.append(row)
        if channel and root:
            self.by_key.setdefault((channel, root), []).append(row)
        elif channel:
            self.by_channel.setdefault(channel, []).append(row)

    def _add_sweep(self, key, entry):
        found = session_ids(key)
        if not found:
            found = session_ids(*(value for name, value in entry.items()
                                  if name not in ("channels", "dms")))
        session = found[0] if found else None
        for name, kind in (("channels", "channel"), ("dms", "dm")):
            values = entry.get(name)
            for value in values if isinstance(values, list) else []:
                conversation = channel_id(value)
                if conversation:
                    self.sweep.setdefault(conversation, []).append(
                        SweepScope(key, kind, conversation, session))

    def _add_rule(self, position, rule):
        if "trigger" in rule or "route" in rule:
            parts = (rule.get("trigger"), rule.get("route"))
        else:
            parts = (rule.get("if"), rule.get("then"))
        raw = " -> ".join(part for part in (text(value) for value in parts) if part)
        if raw:
            self.rules.append(RoutingRule(position, raw, tuple(conversation_ids(raw)),
                                          tuple(session_ids(raw))))

    # ---------- lookups ----------

    @staticmethod
    def order(row):
        """Duplicate precedence key: newest last_ts first, then later in the file."""
        value = ts_value(row.last_ts) if row.last_ts else None
        return (value if value is not None else Decimal("-Infinity"), row.index)

    @classmethod
    def effective(cls, rows):
        """The row that speaks for duplicates: newest last_ts, then later in the file."""
        return max(rows, key=cls.order) if rows else None

    def rows_for(self, channel, root):
        """Every row for (channel, root), in file order."""
        if not channel or not root:
            return []
        return list(self.by_key.get((channel, str(root)), []))

    def channel_rows(self, channel):
        """Rows for the channel without a root (flat channel or DM rows)."""
        return list(self.by_channel.get(channel, [])) if channel else []

    def match(self, channel, ts=None, thread_ts=None):
        """The effective row for a message: (channel, thread_ts) first, then (channel, ts)."""
        for key in (thread_ts, ts):
            row = self.effective(self.rows_for(channel, key))
            if row is not None:
                return row
        return None

    def sweep_for(self, conversation):
        return list(self.sweep.get(conversation, [])) if conversation else []

    def rules_for(self, conversation):
        return [rule for rule in self.rules if conversation in rule.channels] if conversation else []

    # ---------- file loading with an mtime cache ----------

    _cache = {}
    _failed = {}
    _lock = threading.Lock()

    @classmethod
    def load(cls, path, *, closed_statuses=DEFAULT_CLOSED_STATUSES):
        """The index for `path`, parsed once per file (mtime, size) and closed statuses.

        A read or parse failure keeps the previously loaded index (an empty one when none
        was loaded) and logs one WARNING with the exception class only, once per failing
        file version.
        """
        key = (str(path), tuple(closed_statuses))
        with cls._lock:
            previous = cls._cache.get(key)
            try:
                info = Path(path).stat()
                stamp = (info.st_mtime_ns, info.st_size)
            except OSError as error:
                return cls._keep(key, previous, ("missing",), error)
            if previous is not None and previous.stamp == stamp:
                return previous
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                index = cls.parse(data, path=str(path), closed_statuses=closed_statuses,
                                  stamp=stamp)
            except Exception as error:
                return cls._keep(key, previous, stamp, error)
            cls._cache[key] = index
            cls._failed.pop(key, None)
            return index

    @classmethod
    def _keep(cls, key, previous, stamp, error):
        if cls._failed.get(key) != stamp:
            cls._failed[key] = stamp
            LOG.warning("Registry load failed class=%s", type(error).__name__)
        return previous if previous is not None else cls(path=key[0])

    @classmethod
    def clear_cache(cls):
        with cls._lock:
            cls._cache.clear()
            cls._failed.clear()


def lane_for_session(config, session):
    """The configured lane whose `session` starts with the 8 hex session id, else None."""
    if not session:
        return None
    session = session.lower()
    for lane in getattr(config, "lanes", ()):
        value = (lane.session or "").strip().lower()
        if len(value) >= 8 and value[:8] == session[:8] and SESSION_ID.fullmatch(value[:8]):
            return lane.id
    return None


def registry_info(row, message_ts=None):
    """Badge data for a message whose (channel, root or ts) matches a registry row.

    `registry_closed` is true when the row is closed and its `last_ts` is not older than
    the message ts; with no `last_ts`, or an older one, only the badge applies.
    """
    if row is None:
        return None
    info = row.summary()
    newer = None
    if row.last_ts is not None and message_ts is not None:
        last, ts = ts_value(row.last_ts), ts_value(message_ts)
        newer = None if last is None or ts is None else last >= ts
    info["last_ts_newer"] = newer
    info["registry_closed"] = bool(row.closed and newer)
    info["note"] = row.close_note() if info["registry_closed"] else None
    return info
