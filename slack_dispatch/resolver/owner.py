"""Owner resolver (P17): who owns a channel or thread, with the evidence.

`resolve(store, config, registry, channel, ts=None, thread_ts=None)` returns a
`Resolution` whose ordered `candidates` are, tier by tier:

1. `certain`: the ledger binding of the thread, a confirmed scope claim for the thread
   key, the channel or the dm, or the config channel or dm default.
2. `declared`: a registry row for (channel, root) whose session ids map to configured
   lanes, a registry `_meta` sweep scope that lists the conversation, or a registry
   routing rule whose text names the conversation id and a session id.
3. `recorded`: a registry row for (channel, root) with an owner text but no session id.
4. `observed`: lanes that acked or completed ledger items of the conversation within
   `owner_resolver.activity_days`, by count; then the optional harvest jsonl aggregated
   per lane (strong and weak file counts).

`Resolution.best` is the first candidate with a lane. Evidence is trimmed to 160
characters and never contains a ledger message body. The resolver reads local files and
the ledger only; it never calls Slack, a model or the network.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import threading
import time

from .registry import RegistryIndex, lane_for_session, registry_info

LOG = logging.getLogger(__name__)

TIERS = ("certain", "declared", "recorded", "observed")
EVIDENCE_CHARS = 160
REASON_CHARS = 200
VERSION = "owner-resolver/1"
HUMAN_QUEUE = "human_queue"
OPEN_STATES = ("human_pending", "assigned", "acked")


def trim(value, limit=EVIDENCE_CHARS):
    """One line of at most `limit` characters (an ellipsis marks a cut)."""
    value = " ".join(str(value or "").split())
    return value if len(value) <= limit else value[:limit - 3].rstrip() + "..."


@dataclass(frozen=True)
class Candidate:
    tier: str
    lane: str | None
    source: str
    evidence: str
    session: str | None = None

    def as_dict(self):
        return {"lane": self.lane, "session": self.session, "confidence": self.tier,
                "source": self.source, "evidence": self.evidence}


@dataclass
class Resolution:
    channel: str
    ts: str | None = None
    thread_ts: str | None = None
    candidates: list = field(default_factory=list)
    registry_row: dict | None = None

    @property
    def best(self):
        """The first candidate with a lane, or None."""
        return next((candidate for candidate in self.candidates if candidate.lane), None)

    @property
    def confidence(self):
        best = self.best
        return best.tier if best else None

    def reason(self):
        """Suggestion reason `<source>: <evidence>`, one line, at most 200 characters."""
        best = self.best
        return trim(f"{best.source}: {best.evidence}", REASON_CHARS) if best else None

    def as_dict(self):
        best = self.best
        return {"channel": self.channel, "ts": self.ts, "thread_ts": self.thread_ts,
                "root": self.thread_ts or self.ts,
                "best": ({"lane": best.lane, "confidence": best.tier, "source": best.source,
                          "evidence": best.evidence} if best else None),
                "candidates": [candidate.as_dict() for candidate in self.candidates],
                "registry_row": self.registry_row}


# ---------- harvest jsonl (optional) ----------

class HarvestIndex:
    """Per-file rows of the scope harvest jsonl: lane, tier and frequency maps."""

    _cache = {}
    _lock = threading.Lock()

    def __init__(self, rows=()):
        self.rows = list(rows)

    @staticmethod
    def parse_lines(lines):
        rows = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or not isinstance(row.get("lane"), str):
                continue
            maps = {}
            for name in ("channels", "dms", "thread_ts"):
                value = row.get(name)
                maps[name] = value if isinstance(value, dict) else {}
            rows.append({"lane": row["lane"], "strong": str(row.get("tier") or "").startswith("strong"),
                         **maps})
        return rows

    @classmethod
    def load(cls, path):
        """The harvest for `path` (cached per mtime); a missing or unreadable file is empty."""
        if not path:
            return cls()
        with cls._lock:
            try:
                info = Path(path).stat()
                stamp = (info.st_mtime_ns, info.st_size)
            except OSError:
                return cls()
            cached = cls._cache.get(str(path))
            if cached and cached[0] == stamp:
                return cached[1]
            try:
                index = cls(cls.parse_lines(Path(path).read_text(encoding="utf-8").splitlines()))
            except (OSError, UnicodeError) as error:
                LOG.warning("Harvest load failed class=%s", type(error).__name__)
                return cached[1] if cached else cls()
            cls._cache[str(path)] = (stamp, index)
            return index

    def counts(self, conversation, root=None):
        """{lane: [strong, weak]} file counts for files naming the conversation or root."""
        result = {}
        for row in self.rows:
            if conversation in row["channels"] or conversation in row["dms"] or (
                    root and root in row["thread_ts"]):
                entry = result.setdefault(row["lane"], [0, 0])
                entry[0 if row["strong"] else 1] += 1
        return result


# ---------- the resolver ----------

def _parse_stamp(value):
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment


def activity_counts(store, channel, since, lanes):
    """{lane: count} of ledger items of the channel a lane acked or completed since `since`.

    Uses `store.channel_lane_activity` when the store has it (stores without it, such as
    test fakes, add nothing). Only configured lanes count.
    """
    lookup = getattr(store, "channel_lane_activity", None)
    if not callable(lookup):
        return {}
    counts = {}
    for row in lookup(channel):
        lane = row.get("lane")
        if lane not in lanes:
            continue
        stamps = [_parse_stamp(row.get(name)) for name in ("ack_at", "done_at")]
        if any(stamp is not None and stamp >= since for stamp in stamps):
            counts[lane] = counts.get(lane, 0) + 1
    return counts


def _is_dm(channel):
    return str(channel or "").startswith("D")


def _certain(store, config, channel, root):
    found = []
    binding = store.lookup_binding("slack", channel, root) if root else None
    if binding:
        found.append(Candidate("certain", binding, "binding", trim(f"thread {channel}:{root}")))
    lookup = getattr(store, "lookup_scope_claim", None)
    if callable(lookup):
        keys = ([("thread", f"{channel}:{root}")] if root else []) + [
            (kind, channel) for kind in (("dm", "channel") if _is_dm(channel) else ("channel", "dm"))]
        for kind, key in keys:
            claim = lookup(kind, key)
            if claim and claim.get("lane"):
                found.append(Candidate("certain", claim["lane"], "scope_claim",
                                       trim(f"{kind} {key} confirmed by {claim.get('confirmed_by') or '-'}")))
    # The router's ChannelDefault rule: dm defaults for direct conversations.
    source = "dm_default" if _is_dm(channel) or channel in config.dm_defaults else "channel_default"
    table = config.dm_defaults if source == "dm_default" else config.channel_defaults
    if table.get(channel):
        found.append(Candidate("certain", table[channel], source, trim(f"config {source} {channel}")))
    return found


def _row_evidence(row):
    parts = [f"registry {row.id or '-'}", f"status {row.status or '-'}"]
    if row.owner_text():
        parts.append(f"owner {row.owner_text()}")
    if row.title:
        parts.append(f"title {row.title}")
    return trim("; ".join(parts))


def _registry(config, registry, channel, root):
    declared, recorded, shown = [], [], None
    rows = registry.rows_for(channel, root) if registry is not None and root else []
    exact = bool(rows)
    if registry is not None and not rows:
        rows = registry.channel_rows(channel)
    if rows:
        shown = registry.effective(rows)
    for row in sorted(rows, key=RegistryIndex.order, reverse=True):
        source = "registry" if exact else "registry_channel"
        if row.session_ids:
            for session in row.session_ids:
                declared.append(Candidate("declared", lane_for_session(config, session), source,
                                          _row_evidence(row), session))
        elif row.owner_text():
            recorded.append(Candidate("recorded", None, source, _row_evidence(row)))
    if registry is not None:
        for scope in registry.sweep_for(channel):
            declared.append(Candidate("declared", lane_for_session(config, scope.session),
                                      "sweep_scope",
                                      trim(f"_meta.{scope.key} lists {scope.kind} {channel}"),
                                      scope.session))
        for rule in registry.rules_for(channel):
            for session in rule.sessions:
                declared.append(Candidate("declared", lane_for_session(config, session),
                                          "routing_rule", trim(f"rule {rule.index + 1}: {rule.text}"),
                                          session))
    return declared, recorded, shown


def _observed(store, config, channel, root, harvest, now):
    lanes = {lane.id for lane in config.lanes if lane.id != HUMAN_QUEUE}
    settings = getattr(config, "owner_resolver", None)
    days = getattr(settings, "activity_days", 60)
    since = datetime.fromtimestamp(now, timezone.utc) - timedelta(days=days)
    found = []
    counts = activity_counts(store, channel, since, lanes)
    for lane, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        found.append(Candidate("observed", lane, "activity",
                               trim(f"{count} item(s) acked or done in {channel} in {days} days")))
    if harvest is not None:
        totals = {lane: value for lane, value in harvest.counts(channel, root).items() if lane in lanes}
        for lane, (strong, weak) in sorted(totals.items(), key=lambda item: (-item[1][0], -item[1][1], item[0])):
            found.append(Candidate("observed", lane, "harvest",
                                   trim(f"harvest files naming {channel}: strong {strong}, weak {weak}")))
    return found


def resolve(store, config, registry, channel, ts=None, thread_ts=None, *, harvest=None, now=None):
    """Ordered owner candidates for a channel or thread (see the module docstring).

    `registry` is a `RegistryIndex` or None. `harvest` is a `HarvestIndex`; when None it
    is loaded from `owner_resolver.harvest_path` (missing file: no harvest evidence).
    `now` (epoch seconds) bounds the activity window; it defaults to the current time.
    """
    root = thread_ts or ts
    now = time.time() if now is None else now
    if harvest is None:
        harvest = HarvestIndex.load(getattr(getattr(config, "owner_resolver", None), "harvest_path", None))
    resolution = Resolution(channel, ts, thread_ts)
    declared, recorded, shown = _registry(config, registry, channel, root)
    candidates = (_certain(store, config, channel, root) + declared + recorded
                  + _observed(store, config, channel, root, harvest, now))
    unique = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    resolution.candidates = unique
    resolution.registry_row = shown.summary() if shown is not None else None
    return resolution


class OwnerResolver:
    """Config-bound resolver with a refresh-limited registry reload.

    The registry file is checked at most every `owner_resolver.refresh_seconds`
    (`RegistryIndex.load` reparses only when the file changed).
    """

    def __init__(self, config, *, clock=time.monotonic):
        self.config = config
        self.settings = config.owner_resolver
        self.clock = clock
        self._index = None
        self._checked = None
        self._lock = threading.Lock()

    @property
    def enabled(self):
        return bool(self.settings.enabled)

    def registry(self):
        """The current registry index, or None when no registry_path is configured."""
        path = self.settings.registry_path
        if not path:
            return None
        with self._lock:
            now = self.clock()
            if (self._index is None or self._checked is None
                    or now - self._checked >= self.settings.refresh_seconds):
                self._index = RegistryIndex.load(path, closed_statuses=self.settings.closed_statuses)
                self._checked = now
            return self._index

    def resolve(self, store, channel, ts=None, thread_ts=None, *, now=None):
        return resolve(store, self.config, self.registry(), channel, ts, thread_ts, now=now)

    def registry_for(self, message):
        """Registry badge data for a ledger row (see `registry.registry_info`), or None."""
        index = self.registry()
        if index is None or not message.get("channel"):
            return None
        row = index.match(message["channel"], message.get("ts"), message.get("thread_ts"))
        return registry_info(row, message.get("ts"))


def message_root(message):
    return message.get("thread_ts") or message.get("ts")


def suggestion_for(resolver, store, message, *, now=None):
    """(lane, reason) for a message from the resolver, or None when no candidate has a lane."""
    channel = message.get("channel")
    if not channel:
        return None
    resolution = resolver.resolve(store, channel, message.get("ts"), message.get("thread_ts"), now=now)
    best = resolution.best
    return (best.lane, resolution.reason()) if best else None


__all__ = ["Candidate", "HarvestIndex", "OPEN_STATES", "OwnerResolver", "Resolution", "TIERS",
           "VERSION", "message_root", "resolve", "suggestion_for", "trim"]
