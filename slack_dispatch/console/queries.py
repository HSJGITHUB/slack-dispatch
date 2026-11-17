"""Read models for the operator console. Plain Python; no web framework imports.

Every function reads the ledger, the configuration and local files only. Message
bodies are returned to the console page for display and are never logged.
"""
import argparse
from collections import Counter
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import io
import json
import logging
import re
from pathlib import Path

from slack_dispatch.console.actions import seconds_left, undoable
from slack_dispatch.inbox.cli import display_zone, show_inbox, wake_candidates
from slack_dispatch.ingest.membership import member_status
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.ledger.store import delivery_item_ids
from slack_dispatch.notify import effective_lane, wake_switch_path
from slack_dispatch.notify.dispatch_card import permalink as card_permalink
from slack_dispatch.notify.session_resolve import is_session_id
from slack_dispatch.pipeline import HUMAN_QUEUE
from slack_dispatch.queue.human import QUEUE_KINDS, operator_queue
from slack_dispatch.runtime.heartbeat import SCHEMA, is_stale, read_heartbeat

LOG = logging.getLogger(__name__)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
ALL_ROWS = 2**63 - 1  # SQLite's largest LIMIT, as the timer sweep uses.
ROOT_CHARS = 300
CONTEXT_MESSAGES = 5
CONTEXT_CHARS = 300
SNIPPET_CHARS = 200
SEARCH_LIMIT = 200
HINT_HISTORY = 100
MAX_HINTS = 5
LOG_TAIL_BYTES = 1 << 20
RECONCILE_PASSES = 5
WARNING_LINES = 20
GAP_ROWS = 20
WARNING_LEVELS = ("WARNING", "ERROR", "CRITICAL")
BUSY_TIMEOUT_MS = 15000


class ConsoleLedger(LedgerStore):
    """The service's LedgerStore with a 15 second busy timeout (sqlite3 timeout=15)."""

    def open(self):
        super().open()
        with self._lock:
            self._db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")


def open_ledger(config):
    """Open and migrate the configured ledger exactly as the inbox CLI does."""
    store = ConsoleLedger(config.ledger.path, store_body=config.ledger.store_body)
    store.open()
    try:
        store.migrate()
    except Exception:
        store.close()
        raise
    return store


def inbox_root(config):
    """The lane inbox directory the service writes: the first lane file_inbox adapter, else inbox.root."""
    for adapter in config.notify_adapters:
        if adapter.kind == "file_inbox" and adapter.purpose == "lane" and adapter.path:
            return adapter.path
    return config.inbox.root


def lane_ids(config):
    """Configured lanes offered by the console, in configuration order (no human queue pseudo id)."""
    return [lane.id for lane in config.lanes if lane.id != HUMAN_QUEUE]


def direct_lane(config):
    """The configured direct lane, or None when console.direct_lane is not a configured lane."""
    lane = config.console.direct_lane
    return lane if lane in lane_ids(config) else None


def operator_name(config):
    """P25: the display name of `always_human.owner_user` from `people`, or None."""
    owner = getattr(getattr(config, "always_human", None), "owner_user", None)
    return dict(getattr(config, "people", None) or {}).get(owner) if owner else None


def direct_session(store, config):
    """P25: True when a session value (config or ledger claim) is attached to the direct lane.

    Normally the direct lane has none: its items wait in the console queue and the Lanes tab
    shows a link to the queue instead of the lane card."""
    lane = direct_lane(config)
    if lane is None:
        return False
    session, _ = effective_lane(config, store, lane)
    return bool(session)


def ts_key(ts):
    try:
        value = Decimal(str(ts))
        return value if value.is_finite() else Decimal("-Infinity")
    except (InvalidOperation, ValueError):
        return Decimal("-Infinity")


def seconds_since(stamp, now):
    """Whole seconds from an ISO timestamp to now, or None when unusable."""
    try:
        then = datetime.fromisoformat(stamp)
        if then.tzinfo is None:
            then = then.astimezone()
        return max(0, int(now - then.timestamp()))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# P20 display names and permalinks.

GROUP_DM = "group DM"  # the page shows its ko label for this exact value


def message_permalink(domain, channel, ts, thread_ts=None):
    """Slack permalink of a message, or None without a workspace domain (P20).

    `https://<domain>.slack.com/archives/<CHANNEL>/p<ts without the dot>`, built by the
    dispatch card's own helper; a thread reply (thread_ts set and different from ts) adds
    `?thread_ts=<root ts>&cid=<CHANNEL>`.
    """
    link = card_permalink(domain, channel, ts)
    if link and thread_ts and str(thread_ts) != str(ts):
        link += f"?thread_ts={thread_ts}&cid={channel}"
    return link


def workspace_domain(config):
    """`console.workspace_domain`, or None (permalinks hidden)."""
    return getattr(getattr(config, "console", None), "workspace_domain", None) or None


class DisplayNames:
    """The one display name resolver of the console and the inbox CLI (P20).

    Users: config `people`, then `user_directory.name` in the ledger, then the id.
    Channels: `console.channels`, then `channel_membership`: `#<name>` for channels,
    `DM:<partner name>` for `im` rows (partner through the user order above), `group DM`
    for `mpim` rows; then the id. Each ledger table is read at most once per instance, so
    one instance per page build or command keeps it to one query per table.
    """

    def __init__(self, config=None, store=None):
        self.people = dict(getattr(config, "people", None) or {})
        self.channels = dict(getattr(getattr(config, "console", None), "channels", None) or {})
        self.store = store
        self._users = None
        self._conversations = None

    def _read(self, method):
        lookup = getattr(self.store, method, None)
        if not callable(lookup):
            return []
        try:
            return lookup() or []
        except Exception as error:
            # Names are display data: a failed read shows ids instead of failing the page.
            LOG.warning("Display name read failed table=%s class=%s", method, type(error).__name__)
            return []

    def directory(self):
        """{user id: name} from the ledger table `user_directory` (one read)."""
        if self._users is None:
            self._users = {row["user"]: row["name"] for row in self._read("list_user_directory")
                           if row.get("user") and row.get("name")}
        return self._users

    def membership(self):
        """{channel id: row} from the ledger table `channel_membership` (one read)."""
        if self._conversations is None:
            self._conversations = {row["channel"]: row for row in self._read("list_membership")
                                   if row.get("channel")}
        return self._conversations

    def user_name(self, user):
        """The display name of a user id, or None when only the id is known."""
        if not user:
            return None
        return self.people.get(user) or self.directory().get(user)

    def user(self, user):
        return self.user_name(user) or user

    def channel_name(self, channel):
        """The display name of a conversation id, or None when only the id is known."""
        if not channel:
            return None
        if self.channels.get(channel):
            return self.channels[channel]
        row = self.membership().get(channel)
        if row is None:
            return None
        if row.get("kind") == "im":
            partner = self.user(row.get("partner_user"))
            return "DM:" + partner if partner else None
        if row.get("kind") == "mpim":
            return GROUP_DM
        return "#" + row["name"] if row.get("name") else None

    def channel(self, channel):
        return self.channel_name(channel) or channel


class View:
    """Display settings shared by the read models: people, channel names and time zone.

    `View(config)` uses the configured names only. `view.bind(store)` (P20) returns a copy
    for one page build whose names follow `DisplayNames` (config, then ledger, then id).
    """

    def __init__(self, config, store=None):
        warning = io.StringIO()
        self.zone = display_zone(config.display_timezone, warning)
        self.zone_warning = warning.getvalue().strip() or None
        self.config = config
        self.people = dict(config.people)
        self.channels = dict(config.console.channels)
        self.domain = workspace_domain(config)
        self.names = DisplayNames(config, store) if store is not None else None

    def bind(self, store):
        """A copy of this view with a fresh name cache for one page build (P20)."""
        bound = copy.copy(self)
        bound.names = DisplayNames(self.config, store)
        return bound

    def user_name(self, user):
        if not user:
            return None
        return self.names.user_name(user) if self.names is not None else self.people.get(user)

    def channel_name(self, channel):
        if not channel:
            return None
        if self.names is not None:
            return self.names.channel_name(channel)
        return self.channels.get(channel)

    def person(self, user):
        return {"id": user, "name": self.user_name(user)}

    def channel(self, channel):
        return {"id": channel, "name": self.channel_name(channel)}

    def permalink(self, channel, ts, thread_ts=None):
        return message_permalink(self.domain, channel, ts, thread_ts)

    def when_ts(self, ts):
        try:
            moment = EPOCH + timedelta(seconds=float(Decimal(str(ts))))
            return moment.astimezone(self.zone).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, ArithmeticError, OverflowError):
            return None

    def when_epoch(self, seconds):
        if seconds is None:
            return None
        try:
            return datetime.fromtimestamp(float(seconds), self.zone).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    def day(self, stamp):
        try:
            moment = datetime.fromisoformat(stamp)
            if moment.tzinfo is None:
                moment = moment.astimezone()
            return moment.astimezone(self.zone).date()
        except (TypeError, ValueError, OverflowError, OSError):
            return None


def _clip(body, limit):
    if body is None:
        return None, False
    return body[:limit], len(body) > limit


def _brief(view, row, limit):
    """A thread message for context lists: metadata plus a clipped body."""
    body, truncated = _clip(row.get("body"), limit)
    author = view.person(row.get("author"))
    return {"id": row["id"], "ts": row["ts"], "when": view.when_ts(row["ts"]),
            "author": author, "author_name": author["name"] or author["id"],
            "state": row.get("state"), "lane": row.get("lane"), "body": body,
            "truncated": truncated,
            "permalink": view.permalink(row.get("channel"), row["ts"], row.get("thread_ts"))}


def lane_hints(store, config, message):
    """Lane hints from the author: person-channel rules, then the author's recent lanes."""
    author = message.get("author")
    if not config.console.lane_hint_people or not author:
        return []
    allowed = set(lane_ids(config))
    hints, seen = [], set()
    for rule in config.person_channel_rules:
        if rule.person == author and rule.lane in allowed and rule.lane not in seen:
            hints.append({"lane": rule.lane, "source": "rule", "channel": rule.channel})
            seen.add(rule.lane)
    for row in store.author_lane_counts(author, limit=HINT_HISTORY, exclude_id=message["id"]):
        if row["lane"] in allowed and row["lane"] not in seen and len(seen) < MAX_HINTS:
            hints.append({"lane": row["lane"], "source": "history", "count": row["count"]})
            seen.add(row["lane"])
    return hints


def author_hints(store, config, row, cache):
    if cache is None:
        return lane_hints(store, config, row)
    key = row.get("author")
    if key not in cache:
        cache[key] = lane_hints(store, config, row)
    return [dict(hint) for hint in cache[key]]


def membership_view(store, view=None):
    """(member channel ids, loaded) from the ledger table `channel_membership` (P15).

    With a bound view (P20) the rows its name cache already read are reused."""
    if view is not None and getattr(view, "names", None) is not None:
        rows = list(view.names.membership().values())
    else:
        lookup = getattr(store, "list_membership", None)
        rows = lookup() if callable(lookup) else []
    members = {row["channel"] for row in rows if row["is_member"]}
    return members, bool(members)


def not_member(row, membership):
    """True when the membership cache says the item's channel is not a member channel."""
    members, loaded = membership
    return member_status(row.get("channel"), members, loaded, row.get("channel_type")) is False


def registry_view(owner, row):
    """P17 registry badge data for a ledger row, or None (no resolver, no match, failure)."""
    if owner is None:
        return None
    try:
        return owner.registry_for(row)
    except Exception as error:
        LOG.warning("Registry lookup skipped class=%s", type(error).__name__)
        return None


def queue_item(store, config, view, row, now, hints=None, membership=None, owner=None,
               queue_kind="undecided", session_attached=False, session_live=None):
    """One triage item: metadata, full body, thread root and recent thread context.

    P25: `queue_kind` is `undecided` (a `human_pending` item, no lane) or `direct` (an open
    item of the direct lane); a direct row adds `lane` and, with `session_attached` (a
    session is attached to the direct lane), `direct_session: true`. fix1: with
    `session_live` False (the attached session is not live) a direct row also has
    `direct_session_down: true`.

    `hints` caches author lane hints per page (pending items own no lane, so the
    author's lane history is the same for each of their items). `membership` is
    `membership_view(store)`, read once per page (P15 `not_member` badge). `owner` is
    the P17 `OwnerResolver` (or None): it adds the `registry` badge data.
    """
    membership = membership_view(store, view) if membership is None else membership
    full = store.get_message(row["id"], include_body=True) or dict(row)
    root_ts = row.get("thread_ts") or row["ts"]
    is_reply = root_ts != row["ts"]
    thread = store.thread_messages(row["source"], row["channel"], root_ts, include_body=True)
    root = None
    if is_reply:
        root_row = next((item for item in thread if item["ts"] == root_ts), None)
        if root_row is None:
            root = {"id": f"{row['source']}:{row['channel']}:{root_ts}", "ts": root_ts,
                    "when": view.when_ts(root_ts), "author": view.person(None),
                    "author_name": None, "state": None, "lane": None, "body": None,
                    "truncated": False, "missing": True,
                    "permalink": view.permalink(row["channel"], root_ts)}
        else:
            root = dict(_brief(view, root_row, ROOT_CHARS), missing=False)
    others = [item for item in thread if item["ts"] != root_ts and item["id"] != row["id"]]
    context = [dict(_brief(view, item, CONTEXT_CHARS), after=ts_key(item["ts"]) > ts_key(row["ts"]))
               for item in others[-CONTEXT_MESSAGES:]]
    suggestion = None
    if row.get("classifier_suggestion"):
        suggestion = {"lane": row["classifier_suggestion"], "reason": row.get("suggestion_reason"),
                      "version": row.get("classifier_version")}
    channel, author = view.channel(row["channel"]), view.person(row.get("author"))
    return {"id": row["id"], "ts": row["ts"], "when": view.when_ts(row["ts"]),
            "received_at": row.get("received_at"),
            "age_seconds": seconds_since(row.get("received_at"), now),
            "channel": channel, "author": author,
            "channel_name": channel["name"] or channel["id"],
            "author_name": author["name"] or author["id"],
            "permalink": view.permalink(row["channel"], row["ts"], row.get("thread_ts")),
            "attention": row.get("attention"), "origin": row.get("origin"),
            "is_bot": bool(row.get("is_bot")), "has_files": bool(row.get("has_files")),
            "edited": bool(row.get("edited_at")), "followup": bool(row.get("followup")),
            "state": row["state"], "pending_reason": row.get("note"),
            "not_member": not_member(row, membership),
            # P21: a direct message (channel type `im`) gets the DM badge on the page.
            "is_dm": row.get("channel_type") == "im",
            "suggestion": suggestion, "hints": author_hints(store, config, row, hints),
            "is_reply": is_reply, "body": full.get("body"), "root": root,
            "context": context, "thread_size": len(thread),
            "registry": registry_view(owner, row),
            "queue_kind": queue_kind, "lane": row.get("lane"),
            "direct_session": bool(session_attached) and queue_kind == "direct",
            "direct_session_down": (bool(session_attached) and queue_kind == "direct"
                                    and session_live is False)}


QUEUE_FILTERS = ("all",) + QUEUE_KINDS


def queue_rows(store, config):
    """P25 operator queue rows, newest first by message time: (row, queue_kind) pairs.

    `undecided` = `human_pending` (no lane resolved); `direct` = `assigned` or `acked` items
    of the configured direct lane (none when `console.direct_lane` is not a configured lane).
    """
    rows = operator_queue(store, direct_lane(config))
    pairs = [(row, kind) for kind in QUEUE_KINDS for row in rows[kind]]
    pairs.sort(key=lambda pair: (ts_key(pair[0]["ts"]), pair[0]["id"]), reverse=True)
    return pairs


def queue_counts(store, config, pairs=None):
    """`{undecided, direct}` counts of the operator queue (P25 tab label and header)."""
    pairs = queue_rows(store, config) if pairs is None else pairs
    counts = Counter(kind for _, kind in pairs)
    return {kind: counts[kind] for kind in QUEUE_KINDS}


def queue_page(store, config, view, now, *, offset=0, limit=None, owner=None, kind="all",
               session_live=None):
    """The operator queue, newest first by message time, one page at a time (P25).

    Rows are `human_pending` messages (`queue_kind` `undecided`) and open items of the
    direct lane (`queue_kind` `direct`). `kind` (`all`, `undecided`, `direct`) filters the
    page; `counts` (`{undecided, direct}`), `pending_ids` (P14, every queue id of both
    kinds so the page keeps staged rows hidden by the filter) and `registry_closed` always
    cover the whole queue. With the P17 owner resolver, `registry_closed` lists every queue
    item (all pages) whose registry row is closed at or after the item's ts, with the staged
    done note `registry:<status>:<registry id>`. Nothing is written here.

    fix1: `session_live` (the console passes the direct lane identity's `live`) adds
    `direct_session_live` and, for direct rows, `direct_session_down`.
    """
    if kind not in QUEUE_FILTERS:
        raise ValueError("Unknown queue kind")
    limit = config.console.page_size if limit is None else limit
    pairs = queue_rows(store, config)
    shown = [pair for pair in pairs if kind == "all" or pair[1] == kind]
    page = shown[offset:offset + limit]
    hints = {}
    membership = membership_view(store, view)
    attached = direct_session(store, config)
    closed = []
    if owner is not None:
        for row, _ in pairs:
            info = registry_view(owner, row)
            if info and info["registry_closed"]:
                closed.append({"id": row["id"], "note": info["note"]})
    return {"total": len(shown), "offset": offset, "limit": limit, "kind": kind,
            "counts": queue_counts(store, config, pairs),
            "direct_lane": direct_lane(config), "direct_session": attached,
            "direct_session_live": bool(session_live) if attached else None,
            "pending_ids": [row["id"] for row, _ in pairs],
            "registry_closed": closed,
            "items": [queue_item(store, config, view, row, now, hints, membership, owner,
                                 queue_kind=row_kind, session_attached=attached,
                                 session_live=session_live)
                      for row, row_kind in page]}


CHANNEL_ID = re.compile(r"[CDG][A-Z0-9]{2,63}")
TS_VALUE = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,9})?")


def owner_lookup(store, config, owner, channel, ts=None, *, titles=None):
    """P17 `GET /api/owner`: the owner candidates of a channel or a message.

    `ts` may be a message ts or a thread root ts. A ledger message with that ts supplies
    its thread_ts, so a reply resolves by its thread root. Raises ValueError for an
    invalid channel or ts. P21: with `titles` (`session_titles(resolver)`, set only when a
    sessions table is configured) each candidate adds `session_title` (None when unknown).
    """
    if not CHANNEL_ID.fullmatch(channel or ""):
        raise ValueError("Invalid channel")
    if ts is not None and not TS_VALUE.fullmatch(ts):
        raise ValueError("Invalid ts")
    thread_ts = None
    if ts is not None:
        message = store.get_message(f"slack:{channel}:{ts}")
        if message and message.get("thread_ts") and message["thread_ts"] != ts:
            thread_ts = message["thread_ts"]
    result = owner.resolve(store, channel, ts, thread_ts).as_dict()
    result["lanes"] = lane_ids(config)
    if titles is not None:
        for candidate in result.get("candidates") or []:
            candidate["session_title"] = titles(candidate.get("session"))
    return result


def channel_hint(store, config, message):
    """P14: True when an excluded item's channel has no lane default (config or ledger).

    The console then suggests the Scope tab; nothing here changes routing. Direct
    messages and channels already excluded by the config never get a hint.
    """
    channel = (message or {}).get("channel")
    if not channel or message.get("channel_type") in ("im", "mpim"):
        return False
    if channel in config.excluded_channels or channel in ledger_exclusions(store):
        return False
    current = current_default(store, config, "channel", channel)
    return current is None or current["lane"] is None


def session_state(resolver, lane, session):
    """Session value and liveness, reusing the service's SessionResolver."""
    if not session:
        return {"value": None, "addressed_by": None, "live": False, "state": "no_session",
                "target": None}
    if is_session_id(session):
        target = resolver.resolve_target(lane, session, warn_duplicates=False)
        return {"value": session, "addressed_by": "id", "live": target is not None,
                "state": resolver.lane_state.get(lane), "target": target}
    # Names pass through to the adapter unresolved; report whether a live session file has it.
    live = any(name == session for _, name in resolver.sessions())
    return {"value": session, "addressed_by": "name", "live": live,
            "state": "live" if live else "not_live", "target": session}


# P21 lane session identity and wake now.

WAKE_REFUSALS = {
    "unregistered": "Lane is not in the ledger",
    "paused": "Lane is paused",
    "no_session": "Lane has no session",
    "not_live": "Lane session is not live",
    "ambiguous": "Lane session id matches several live sessions",
    "wake_off": "Wakes are switched off (wake.off or wake_policy.enabled false)",
}


def session_identity(resolver, session, paused=False):
    """`SessionResolver.describe` of a lane session value (P21), or a minimal fallback for
    resolvers without it (test doubles)."""
    describe = getattr(resolver, "describe", None)
    if callable(describe):
        return describe(session, paused=bool(paused))
    return {"id8": None, "id": None, "value": session or None, "addressed_by": None,
            "name": None, "live": False, "title": None,
            "state": "no_session" if not session else ("paused" if paused else "not_live"),
            "table_state": None}


def session_titles(resolver):
    """`resolver.title` when a sessions table is configured (P21), else None. Payloads add
    title keys only when this is set, so their shape is unchanged without a table."""
    if getattr(resolver, "table", None) is None or not callable(getattr(resolver, "title", None)):
        return None
    return resolver.title


def wake_refusal(config, store, resolver, lane):
    """(code, reason) when `Wake now` must be refused for the lane, else None (P21).

    Refused: lane not in the ledger, paused, no session value, a session that is not live
    (or an id prefix matching several live sessions), and wakes switched off (`wake.off`
    or `wake_policy.enabled: false`), since the service would drop the request then.
    """
    if store.get_lane(lane) is None:
        return "unregistered", WAKE_REFUSALS["unregistered"]
    session, paused = effective_lane(config, store, lane)
    if paused:
        return "paused", WAKE_REFUSALS["paused"]
    identity = session_identity(resolver, session)
    if identity["state"] == "no_session":
        return "no_session", WAKE_REFUSALS["no_session"]
    if not identity["live"]:
        code = "ambiguous" if identity["state"] == "ambiguous" else "not_live"
        return code, WAKE_REFUSALS[code]
    if not config.wake_policy.enabled or wake_switch_path(config).exists():
        return "wake_off", WAKE_REFUSALS["wake_off"]
    return None


def lane_wake_info(config, store, resolver, lane):
    """`{allowed, reason, detail, assigned, acked}` for the lane header button (P21)."""
    counts = Counter(row["state"] for row in wake_candidates(store, lane))
    refusal = wake_refusal(config, store, resolver, lane)
    return {"allowed": refusal is None, "reason": refusal[0] if refusal else None,
            "detail": refusal[1] if refusal else None,
            "assigned": counts["assigned"], "acked": counts["acked"]}


def lane_identities(store, config, resolver):
    """{lane: describe result} for every configured lane (queue lane button titles, P21)."""
    result = {}
    for lane in lane_ids(config):
        session, paused = effective_lane(config, store, lane)
        result[lane] = session_identity(resolver, session, paused)
    return result


def lane_card(store, config, view, resolver, root, lane, now):
    """One lane: session, pause, today's counts, open work and delivery times.

    P21 adds `identity` (`SessionResolver.describe`: title, live name, id8, state) and
    `wake` (`lane_wake_info`: whether `Wake now` is allowed and why not)."""
    session, paused = effective_lane(config, store, lane)
    today = datetime.fromtimestamp(now, view.zone).date()
    today_counts = {"new": 0, "assigned": 0, "acked": 0, "done": 0}
    columns = {"new": "received_at", "assigned": "assigned_at", "acked": "ack_at", "done": "done_at"}
    open_count, oldest = 0, None
    for row in store.lane_activity(lane):
        for key, column in columns.items():
            if row.get(column) and view.day(row[column]) == today:
                today_counts[key] += 1
        if row["state"] in ("new", "assigned", "acked"):
            open_count += 1
        if row["state"] == "assigned" and not row.get("ack_at"):
            age = seconds_since(row.get("assigned_at") or row.get("received_at"), now)
            if age is not None and (oldest is None or age > oldest):
                oldest = age
    try:
        size = (Path(root) / (lane + ".jsonl")).stat().st_size
    except OSError:
        size = None
    delivery = store.last_lane_delivery(lane)
    reminder = store.last_lane_reminder(lane)
    return {"lane": lane, "registered": store.get_lane(lane) is not None,
            "session": session_state(resolver, lane, session), "paused": bool(paused),
            "today": today_counts, "open": open_count, "oldest_unacked_seconds": oldest,
            "last_delivery": view.when_epoch(delivery), "last_delivery_epoch": delivery,
            "last_reminder": view.when_epoch(reminder), "last_reminder_epoch": reminder,
            "inbox_bytes": size, "identity": session_identity(resolver, session, paused),
            "wake": lane_wake_info(config, store, resolver, lane)}


def lane_cards(store, config, view, resolver, root, now):
    """Every configured lane card. P25: the direct lane card carries `direct: true` and
    `listed` (false while no session is attached; the Lanes tab then links to the queue)."""
    direct = direct_lane(config)
    attached = direct_session(store, config)
    cards = []
    for lane in lane_ids(config):
        card = lane_card(store, config, view, resolver, root, lane, now)
        card["direct"] = lane == direct
        card["listed"] = lane != direct or attached
        # fix1: a session value is attached to the direct lane but that session is not live.
        card["session_down"] = (lane == direct and attached
                                and not (card.get("identity") or {}).get("live"))
        cards.append(card)
    return cards


def direct_lane_info(store, config, session_live=None):
    """P25 `GET /api/lanes` `direct_lane`: `{lane, session_attached, listed, queue_count,
    session_down}`, or None without a configured direct lane. fix1: `session_down` is true
    when a session is attached but `session_live` is False."""
    lane = direct_lane(config)
    if lane is None:
        return None
    attached = direct_session(store, config)
    return {"lane": lane, "session_attached": attached, "listed": attached,
            "queue_count": queue_counts(store, config)["direct"],
            "session_down": attached and session_live is False}


def lane_inbox(store, config, view, root, lane, *, full=False, owner=None):
    """The lane inbox as thread groups, produced by the CLI's own `inbox LANE --thread --json`.

    With the P17 owner resolver each group and item carries `registry` badge data."""
    args = argparse.Namespace(lane=lane, include_done=False, thread=True, json=True, full=full,
                              no_body=False, show_body=False,
                              owner=config.always_human.owner_user)
    output = io.StringIO()
    show_inbox(args, root, store, output, view.people, view.zone, names=view.names)
    groups = json.loads(output.getvalue() or "[]")
    for group in groups:
        group["root"]["pending"] = "kind" in group["root"]
        group["root"]["channel_name"] = view.channel_name(group["root"].get("channel"))
        group["root"]["when_full"] = view.when_ts(group["root"].get("ts"))
        root_row = group["root"]
        if view.domain:
            # P20: the root line links to the thread root (keys only with a domain, so the
            # root keeps the CLI JSON shape otherwise).
            root_row["permalink"] = view.permalink(root_row.get("channel"), root_row.get("ts"))
        group["registry"] = registry_view(owner, {"channel": root_row.get("channel"),
                                                  "ts": root_row.get("ts")}) if owner else None
        for item in group["items"]:
            message = store.get_message(item["id"]) or {}
            item["state"] = message.get("state")
            item["lane"] = message.get("lane")
            item["when_full"] = view.when_ts(item.get("ts"))
            item["channel"] = message.get("channel") or root_row.get("channel")
            item["channel_name"] = view.channel_name(item["channel"])
            if view.domain:
                item["permalink"] = view.permalink(item["channel"], item.get("ts"),
                                                   message.get("thread_ts") or root_row.get("ts"))
            item["registry"] = registry_view(owner, message) if owner and message else None
        if "owner_replied_at" in group:
            group["owner_replied_when"] = view.when_ts(group["owner_replied_at"])
    return groups


def tail_lines(path, max_bytes=LOG_TAIL_BYTES):
    """Last lines of a text file (read-only); a partial first line is dropped."""
    try:
        with open(path, "rb") as stream:
            stream.seek(0, 2)
            start = max(0, stream.tell() - max_bytes)
            stream.seek(start)
            data = stream.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if start and lines else lines


# The service log format: "%(asctime)s %(levelname)s %(name)s %(message)s".
LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) ([A-Z]+) (\S+) (.*)$")


def parse_log_line(line):
    match = LOG_LINE.match(line)
    if not match:
        return None
    at, level, logger, message = match.groups()
    return {"at": at, "level": level, "logger": logger, "message": message}


def reconcile_summary(entry):
    counts = {key: int(value) for key, value in re.findall(r"(\w+)=(\d+)", entry["message"])}
    backfill = re.search(r"backfill delivered (\d+)", entry["message"])
    if backfill:
        counts["backfill"] = int(backfill.group(1))
    return {"at": entry["at"], "counts": counts}


def service_status(store, config, view, now):
    """Heartbeat, log summaries, gaps, wake switch and adapter dry-run flags. Read-only."""
    heartbeat = read_heartbeat(Path(config.service.state_dir) / "heartbeat.json")
    if heartbeat is not None:
        # Stale after three missed intervals (at least 90 seconds).
        stale = is_stale(heartbeat, now, max(90, 3 * config.service.heartbeat_seconds))
        keys = ("status", "ok", "pid", "version", "ts", "started_at", "last_event_at",
                "last_catchup_at", "gaps", "counters")
        heartbeat = {key: heartbeat.get(key) for key in keys}
        heartbeat["stale"] = stale
        heartbeat["age_seconds"] = seconds_since(heartbeat.get("ts"), now)
    entries = [entry for entry in map(parse_log_line,
                                      tail_lines(Path(config.service.log_dir) / "service.log"))
               if entry]
    passes = [reconcile_summary(entry) for entry in entries
              if entry["message"].startswith("Reconcile pass ")][-RECONCILE_PASSES:]
    warnings = [entry for entry in entries if entry["level"] in WARNING_LEVELS][-WARNING_LINES:]
    adapters = [{"index": index, "kind": adapter.kind, "purpose": adapter.purpose,
                 "dry_run": adapter.dry_run}
                for index, adapter in enumerate(config.notify_adapters)]
    return {"now": view.when_epoch(now), "heartbeat_schema": SCHEMA, "heartbeat": heartbeat,
            "reconcile_passes": list(reversed(passes)), "warnings": list(reversed(warnings)),
            "gaps": store.list_gaps(GAP_ROWS), "wake_off": wake_switch_path(config).exists(),
            "wake_enabled_in_config": config.wake_policy.enabled,
            "human_queue": {"mode": config.human_queue.mode,
                            "digest_seconds": config.human_queue.digest_seconds,
                            "adapters": [row for row in adapters if row["purpose"] == HUMAN_QUEUE]},
            "adapters": adapters}


def search(store, view, query, *, limit=SEARCH_LIMIT):
    """Where did that message go: id substring, channel id, author id or stored body substring."""
    rows = store.search_messages(query, limit=limit + 1)
    result = []
    for row in rows[:limit]:
        snippet, truncated = _clip(row.get("body"), SNIPPET_CHARS)
        channel, author = view.channel(row["channel"]), view.person(row.get("author"))
        result.append({"id": row["id"], "ts": row["ts"], "when": view.when_ts(row["ts"]),
                       "channel": channel, "author": author,
                       "channel_name": channel["name"] or channel["id"],
                       "author_name": author["name"] or author["id"],
                       "permalink": view.permalink(row["channel"], row["ts"], row.get("thread_ts")),
                       "state": row["state"],
                       "lane": row.get("lane"), "route_reason": row.get("route_reason"),
                       "note": row.get("note"), "suggestion": row.get("classifier_suggestion"),
                       "snippet": snippet, "truncated": truncated})
    return {"query": query, "rows": result, "truncated": len(rows) > limit}


# P13 scope learning: proposals tab and learned scope overrides.

PROPOSAL_ORDER = {"suggested": 0, "open": 1}
LIVE_STATUSES = ("open", "suggested", "confirmed")


def scope_name(view, kind, key):
    """Display name of a scope key: people names for persons, channel names otherwise
    (P20: through the bound view's DisplayNames when there is one)."""
    return view.user_name(key) if kind == "person" else view.channel_name(key)


def dm_partner(view, kind, key):
    """P21: `{id, name}` of the person on the other side of a DM scope key (the
    `channel_membership` partner of an `im` row, named through the user order), `{id: None,
    name: None}` when unknown, and None for keys that are not DMs."""
    if kind != "dm":
        return None
    names = getattr(view, "names", None)
    row = names.membership().get(key) if names is not None else None
    partner = row.get("partner_user") if row and row.get("kind") == "im" else None
    return {"id": partner, "name": view.user_name(partner) if partner else None}


def current_default(store, config, kind, key):
    """The lane that routes this key today: a ledger override first, then the config."""
    claim = store.lookup_scope_claim(kind, key)
    if claim:
        source = "scope:auto" if claim.get("confirmed_by") == "scope:auto" else "ledger"
        return {"lane": claim["lane"], "source": source}
    table = {"channel": config.channel_defaults, "dm": config.dm_defaults}.get(kind, {})
    if key in table:
        return {"lane": table[key], "source": "config"}
    return None


def proposals(store, config, view):
    """Open and suggested proposals with evidence, the lanes involved per key, the current
    default, plus the learned scope panel (active confirmed claims)."""
    every = store.list_scope_proposals()
    involved = {}
    for row in every:
        if row["status"] in LIVE_STATUSES:
            involved.setdefault((row["kind"], row["key"]), []).append(
                {"lane": row["lane"], "evidence_count": row["evidence_count"], "status": row["status"]})
    items = []
    for row in every:
        if row["status"] not in PROPOSAL_ORDER:
            continue
        current = current_default(store, config, row["kind"], row["key"])
        items.append({"kind": row["kind"], "key": row["key"],
                      "name": scope_name(view, row["kind"], row["key"]), "lane": row["lane"],
                      "evidence_count": row["evidence_count"], "first_seen": row["first_seen"],
                      "last_seen": row["last_seen"], "status": row["status"],
                      "conflict": bool(row["conflict"]), "last_evidence": row["last_evidence"],
                      "imported": bool(row["imported"]),
                      "partner": dm_partner(view, row["kind"], row["key"]),
                      "lanes": involved.get((row["kind"], row["key"]), []), "current": current,
                      "effective": bool(current and current["source"] != "config"
                                        and current["lane"] == row["lane"])})
    items.sort(key=lambda item: (PROPOSAL_ORDER[item["status"]], -item["evidence_count"],
                                 item["kind"], item["key"], item["lane"]))
    return {"min_evidence": config.scope_learning.min_evidence, "proposals": items,
            "learned": learned_scope(store, config, view),
            "channel_exclusions": channel_exclusions(store, config, view)}


def ledger_exclusions(store):
    """Channels with an active ledger exclusion (P15); empty for stores without the table."""
    lookup = getattr(store, "channel_exclusion_ids", None)
    return lookup() if callable(lookup) else set()


def channel_exclusions(store, config, view):
    """Active ledger channel exclusions (P15) for the Learned scope panel."""
    lookup = getattr(store, "list_channel_exclusions", None)
    rows = lookup() if callable(lookup) else []
    return [{"channel": row["channel"], "name": view.channel_name(row["channel"]),
             "excluded_at": row["excluded_at"], "by": row["by"],
             "in_config": row["channel"] in config.excluded_channels} for row in rows]


def export_exclusions_yaml(store, config, view):
    """The config `excluded_channels` plus the ledger exclusions as one YAML fragment (P15)."""
    ledger = [row["channel"] for row in channel_exclusions(store, config, view)]
    lines = ["# slack-dispatch learned channel exclusions (ledger overrides, P15).",
             "# Replace excluded_channels in the instance config with this list."]
    channels = list(dict.fromkeys(list(config.excluded_channels) + ledger))
    lines.append("excluded_channels:" + ("" if channels else " []"))
    for channel in channels:
        added = channel not in config.excluded_channels
        notes = [note for note in (view.channel_name(channel), "ledger" if added else None) if note]
        lines.append(f"  - {channel}" + (f"  # {', '.join(notes)}" if notes else ""))
    if not ledger:
        lines.append("# No ledger channel exclusions.")
    return "\n".join(lines) + "\n"


def learned_scope(store, config, view):
    rows = []
    for claim in store.confirmed_scope_claims():
        table = {"channel": config.channel_defaults, "dm": config.dm_defaults}.get(claim["kind"], {})
        rows.append({"kind": claim["kind"], "key": claim["key"], "lane": claim["lane"],
                     "name": scope_name(view, claim["kind"], claim["key"]),
                     "partner": dm_partner(view, claim["kind"], claim["key"]),
                     "confirmed_by": claim["confirmed_by"], "confirmed_at": claim["confirmed_at"],
                     "in_config": claim["key"] in table and table[claim["key"]] == claim["lane"]})
    return rows


def export_yaml(store, config, view):
    """The learned overrides as a YAML fragment to merge into the instance config."""
    rows = learned_scope(store, config, view)
    lines = ["# slack-dispatch learned scope (ledger overrides, P13).",
             "# Merge these entries into the matching sections of the instance config."]
    for kind, section in (("channel", "channel_defaults"), ("dm", "dm_defaults")):
        entries = [row for row in rows if row["kind"] == kind]
        if entries:
            lines.append(section + ":")
            for row in entries:
                notes = [note for note in ("scope:auto" if row["confirmed_by"] == "scope:auto" else None,
                                           "same as config" if row["in_config"] else None) if note]
                lines.append(f"  {row['key']}: {{default: {row['lane']}}}"
                             + (f"  # {', '.join(notes)}" if notes else ""))
    persons = [row for row in rows if row["kind"] == "person"]
    if persons:
        lines.append("# Person claims route direct messages from a person; no YAML key exists:")
        lines.extend(f"#   {row['key']}: {row['lane']}" for row in persons)
    if not rows:
        lines.append("# No learned scope overrides.")
    return "\n".join(lines) + "\n"


# P18 delivery log: the Deliveries tab and the inbox CLI `deliveries` command.

DELIVERY_STATUS_FILTERS = ("sent", "failed", "skipped", "skipped_no_session", "skipped_ambiguous",
                           "skipped_paused", "dry_run", "timeout")
DELIVERY_PURPOSES = ("lane", HUMAN_QUEUE)
DELIVERY_DEFAULT_HOURS = 48
RELAY_OUTCOMES = ("delivered", "not_delivered", "unknown")


def delivery_since(now, hours):
    """UTC ISO lower bound for a window of `hours` before now; None for 0 (all rows)."""
    if not hours:
        return None
    moment = datetime.fromtimestamp(max(0.0, float(now) - hours * 3600), timezone.utc)
    return moment.isoformat(timespec="microseconds")


def relay_outcome(detail):
    """The relay outcome prefix of a delivery_log detail (`delivered | ...`), or None."""
    head = (detail or "").split(" | ", 1)[0].strip()
    return head if head in RELAY_OUTCOMES else None


def _clock_time(view, stamp):
    """HH:MM of an ISO stamp in the display zone, or None."""
    try:
        moment = datetime.fromisoformat(stamp)
        if moment.tzinfo is None:
            moment = moment.astimezone()
        return moment.astimezone(view.zone).strftime("%H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _full_time(view, stamp):
    try:
        moment = datetime.fromisoformat(stamp)
        if moment.tzinfo is None:
            moment = moment.astimezone()
        return moment.astimezone(view.zone).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


PREVIEW_CHARS = 120
NO_BODY_STORED = "[no body stored]"
MESSAGE_READ_CHUNK = 500


def delivery_preview(body):
    """(preview, truncated) of a stored body for a Deliveries item (P28).

    Whitespace runs collapse to one space, then the first PREVIEW_CHARS characters are kept.
    A missing or blank body gives NO_BODY_STORED (truncated False)."""
    collapsed = " ".join(str(body).split()) if body is not None else ""
    if not collapsed:
        return NO_BODY_STORED, False
    return collapsed[:PREVIEW_CHARS], len(collapsed) > PREVIEW_CHARS


def delivery_messages(store, ids):
    """{id: messages row with body} for the ids present in the ledger (P28).

    One read-only SELECT per MESSAGE_READ_CHUNK ids through the ledger store's query helper,
    so a Deliveries page reads its items once, not once per row. A store without that helper
    (a test double) falls back to `get_message` per id."""
    ids = list(dict.fromkeys(str(item) for item in ids or ()))
    reader = getattr(store, "_rows", None)
    if not callable(reader):
        rows = {}
        for message_id in ids:
            row = store.get_message(message_id, include_body=True)
            if row is not None:
                rows[message_id] = row
        return rows
    rows = {}
    for start in range(0, len(ids), MESSAGE_READ_CHUNK):
        chunk = ids[start:start + MESSAGE_READ_CHUNK]
        marks = ", ".join("?" for _ in chunk)
        for row in reader(f"SELECT * FROM messages WHERE id IN ({marks})", chunk):
            rows[row["id"]] = row
    return rows


def _delivery_item_value(view, message_id, row):
    """The display object of one covered message (P18 fields plus the P28 content fields)."""
    if row is None:
        parts = message_id.split(":", 2)
        channel = parts[1] if len(parts) == 3 else None
        ts = parts[2] if len(parts) == 3 else None
        thread_ts, author, state, lane, attention, body = None, None, None, None, None, None
        missing = True
    else:
        channel, ts, thread_ts = row["channel"], row["ts"], row.get("thread_ts")
        author, state, lane = row.get("author"), row.get("state"), row.get("lane")
        attention, body, missing = row.get("attention"), row.get("body"), False
    channel_ref, author_ref = view.channel(channel), view.person(author)
    preview, truncated = delivery_preview(body)
    return {"id": message_id, "missing": missing, "channel": channel_ref,
            "channel_name": channel_ref["name"] or channel_ref["id"],
            "is_reply": bool(thread_ts and thread_ts != ts),
            "author": author_ref, "author_name": author_ref["name"] or author_ref["id"],
            "when": view.when_ts(ts) if ts else None,
            "attention": attention, "preview": preview, "preview_truncated": truncated,
            "state": state, "lane": lane,
            "permalink": view.permalink(channel, ts, thread_ts)}


def prefetch_delivery_items(store, view, ids, cache):
    """Fill `cache` ({id: item}) for every id not cached yet with one ledger read (P28)."""
    wanted = [message_id for message_id in dict.fromkeys(ids) if message_id not in cache]
    if not wanted:
        return cache
    rows = delivery_messages(store, wanted)
    for message_id in wanted:
        cache[message_id] = _delivery_item_value(view, message_id, rows.get(message_id))
    return cache


def delivery_item(store, view, message_id, cache):
    """One covered message: channel, author, thread flag, state, message time, attention flag
    and a body preview (P28; first PREVIEW_CHARS characters, never the whole body)."""
    if message_id not in cache:
        prefetch_delivery_items(store, view, [message_id], cache)
    return dict(cache[message_id])


def delivery_row(store, view, row, cache, titles=None):
    """A delivery_log row with receipt fields (from `delivery_receipts`) for display.

    P21: with `titles` (only when a sessions table is configured) the target adds `title`."""
    session_id, name = row.get("target_session_id"), row.get("target_session_name")
    target = None
    if session_id or name:
        target = {"session_id": session_id, "short_id": session_id[:8] if session_id else None,
                  "name": name}
        if titles is not None:
            target["title"] = titles(session_id) if session_id else None
    ids = delivery_item_ids(row.get("item_ids"))
    prefetch_delivery_items(store, view, ids, cache)  # no read when the page prefetched them
    return {"id": row["id"], "created_at": row.get("created_at"),
            "when": _full_time(view, row.get("created_at")),
            "time": _clock_time(view, row.get("created_at")),
            "purpose": row.get("purpose"), "kind": row.get("kind"), "lane": row.get("lane"),
            "adapter": row.get("adapter"), "target": target,
            "item_count": row.get("item_count") if row.get("item_count") is not None else len(ids),
            "items": [delivery_item(store, view, mid, cache) for mid in ids],
            "status": row.get("status"), "relay_outcome": relay_outcome(row.get("detail")),
            "detail": row.get("detail"), "cost_usd": row.get("cost_usd"),
            "duration_ms": row.get("duration_ms"), "relay_session_id": row.get("relay_session_id"),
            "repeat_count": row.get("repeat_count") or 1, "last_seen_at": row.get("last_seen_at"),
            "last_seen_time": _clock_time(view, row.get("last_seen_at")),
            "receipt": {"received_at": row.get("received_at"),
                        "received_time": _clock_time(view, row.get("received_at")),
                        "latency_seconds": row.get("latency_seconds"),
                        "age_seconds": row.get("age_seconds"),
                        "decision_at": row.get("decision_at"),
                        "decision_time": _clock_time(view, row.get("decision_at"))}}


def deliveries_page(store, config, view, now, *, lane=None, purpose=None, status=None,
                    hours=DELIVERY_DEFAULT_HOURS, page=1, limit=None, titles=None):
    """delivery_log rows newest first with receipts, `limit` (default page_size) per page.

    `titles` (P21, `session_titles(resolver)`) adds the session title to each target. P28:
    the items of every row on the page are read from the ledger in one query."""
    limit = config.console.page_size if limit is None else limit
    since = delivery_since(now, hours)
    filters = dict(since=since, lane=lane or None, purpose=purpose or None, status=status or None)
    total = store.count_deliveries(**filters)
    rows = store.list_deliveries(**filters, limit=limit, offset=(page - 1) * limit)
    cache = {}
    # P28: one ledger read for every item of the page (not one per row or per item).
    prefetch_delivery_items(store, view, [mid for row in rows
                                          for mid in delivery_item_ids(row.get("item_ids"))], cache)
    result = {"hours": hours, "page": page, "page_size": limit, "total": total,
              "rows": [delivery_row(store, view, row, cache, titles)
                       for row in store.delivery_receipts(rows, now)]}
    result.update(delivery_log_start(store, view, now, hours=hours, lane=lane, purpose=purpose,
                                     status=status, empty=not total))
    return result


def delivery_log_start(store, view, now, *, hours, lane=None, purpose=None, status=None,
                       empty=True):
    """P20 Deliveries empty state: when the log started and, for an empty window, how many
    `service_deliveries` rows (the pre-P18 record: thread and lane only) fall in the window
    before that start.

    Returns `log_started_at` (UTC ISO or None), `log_started_when` (display time zone) and
    `legacy_deliveries`: 0 unless `empty`, and 0 for the human_queue purpose or lane and for
    statuses other than sent, since that table records lane wakes that were sent.
    """
    lookup = getattr(store, "delivery_log_started_at", None)
    started = lookup() if callable(lookup) else None
    legacy = 0
    counter = getattr(store, "count_service_deliveries", None)
    if (empty and callable(counter) and purpose in (None, "lane") and status in (None, "sent")
            and lane != HUMAN_QUEUE):
        since = max(0.0, float(now) - hours * 3600) if hours else None
        before = None
        if started:
            try:
                moment = datetime.fromisoformat(started)
                before = (moment if moment.tzinfo else moment.astimezone()).timestamp()
            except ValueError:
                before = None
        if before is None or since is None or since < before:
            legacy = counter(since=since, before=before, lane=lane)
    return {"log_started_at": started, "log_started_when": _full_time(view, started),
            "legacy_deliveries": legacy}


def deliveries_summary(store, now, *, hours=DELIVERY_DEFAULT_HOURS):
    """Counts in the window: sent, failed (timeouts included), timeout, skipped, dry_run,
    not_delivered (sent but the relay said not delivered) and unconfirmed (lane rows sent
    without a receipt)."""
    rows = store.delivery_receipts(store.list_deliveries(since=delivery_since(now, hours)), now)
    statuses = Counter(row.get("status") for row in rows)
    return {"hours": hours, "total": len(rows), "sent": statuses["sent"],
            "failed": statuses["failed"] + statuses["timeout"], "timeout": statuses["timeout"],
            "skipped": sum(count for key, count in statuses.items()
                           if str(key).startswith("skipped_")),
            "dry_run": statuses["dry_run"],
            "not_delivered": sum(row.get("status") == "sent"
                                 and relay_outcome(row.get("detail")) == "not_delivered"
                                 for row in rows),
            "unconfirmed": sum(row.get("purpose") == "lane" and row.get("status") == "sent"
                               and row.get("received_at") is None for row in rows)}


# P19 console undo: recent journal rows for the Service tab.

def recent_actions(store, view, now, *, limit=20):
    """The newest `console_actions` rows with `undoable` and `seconds_left` at `now`.

    Each row: id, at, when (display time zone), action, lane, count, ids, by, undo_until,
    undone_at, undoable, seconds_left, the stored undo `result` (null until undone) and
    (P20) `links`: `{id, permalink}` for the first RECENT_LINKS ids when permalinks are on."""
    rows = []
    actions = store.list_console_actions(limit)
    refs = {}
    if view.domain and callable(getattr(store, "message_refs", None)):
        wanted = []
        for row in actions:
            ids = _json_value(row.get("ids"), [])
            wanted.extend(ids[:RECENT_LINKS] if isinstance(ids, list) else [])
        refs = store.message_refs(wanted)
    for row in actions:
        ids = _json_value(row.get("ids"), [])
        meta = _json_value(row.get("meta"), {})
        rows.append({"id": row["id"], "at": row.get("at"), "when": _full_time(view, row.get("at")),
                     "time": _clock_time(view, row.get("at")), "action": row.get("action"),
                     "lane": meta.get("lane") if isinstance(meta, dict) else None,
                     "count": len(ids) if isinstance(ids, list) else 0,
                     "ids": ids if isinstance(ids, list) else [], "by": row.get("by"),
                     "undo_until": row.get("undo_until"), "undone_at": row.get("undone_at"),
                     "undoable": undoable(row, now), "seconds_left": seconds_left(row, now),
                     "result": _json_value(row.get("result"), None),
                     "links": action_links(view, ids, refs)})
    return rows


RECENT_LINKS = 5


def action_links(view, ids, refs):
    """`{id, permalink}` for the first RECENT_LINKS message ids of a journal row (P20)."""
    if not view.domain or not isinstance(ids, list):
        return []
    links = []
    for message_id in ids[:RECENT_LINKS]:
        ref = refs.get(message_id)
        if ref is None:
            parts = str(message_id).split(":", 2)
            ref = {"channel": parts[1], "ts": parts[2]} if len(parts) == 3 else {}
        link = view.permalink(ref.get("channel"), ref.get("ts"), ref.get("thread_ts"))
        if link:
            links.append({"id": message_id, "permalink": link})
    return links


def _json_value(value, default):
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
