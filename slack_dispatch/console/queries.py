"""Read models for the operator console. Plain Python; no web framework imports.

Every function reads the ledger, the configuration and local files only. Message
bodies are returned to the console page for display and are never logged.
"""
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import io
import json
import logging
import re
from pathlib import Path

from slack_dispatch.inbox.cli import display_zone, show_inbox
from slack_dispatch.ingest.membership import member_status
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify import effective_lane, wake_switch_path
from slack_dispatch.notify.session_resolve import is_session_id
from slack_dispatch.pipeline import HUMAN_QUEUE
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


class View:
    """Display settings shared by the read models: people, channel names and time zone."""

    def __init__(self, config):
        warning = io.StringIO()
        self.zone = display_zone(config.display_timezone, warning)
        self.zone_warning = warning.getvalue().strip() or None
        self.people = dict(config.people)
        self.channels = dict(config.console.channels)

    def person(self, user):
        return {"id": user, "name": self.people.get(user) if user else None}

    def channel(self, channel):
        return {"id": channel, "name": self.channels.get(channel) if channel else None}

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
    return {"id": row["id"], "ts": row["ts"], "when": view.when_ts(row["ts"]),
            "author": view.person(row.get("author")), "state": row.get("state"),
            "lane": row.get("lane"), "body": body, "truncated": truncated}


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


def membership_view(store):
    """(member channel ids, loaded) from the ledger table `channel_membership` (P15)."""
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


def queue_item(store, config, view, row, now, hints=None, membership=None, owner=None):
    """One triage item: metadata, full body, thread root and recent thread context.

    `hints` caches author lane hints per page (pending items own no lane, so the
    author's lane history is the same for each of their items). `membership` is
    `membership_view(store)`, read once per page (P15 `not_member` badge). `owner` is
    the P17 `OwnerResolver` (or None): it adds the `registry` badge data.
    """
    membership = membership_view(store) if membership is None else membership
    full = store.get_message(row["id"], include_body=True) or dict(row)
    root_ts = row.get("thread_ts") or row["ts"]
    is_reply = root_ts != row["ts"]
    thread = store.thread_messages(row["source"], row["channel"], root_ts, include_body=True)
    root = None
    if is_reply:
        root_row = next((item for item in thread if item["ts"] == root_ts), None)
        if root_row is None:
            root = {"id": f"{row['source']}:{row['channel']}:{root_ts}", "ts": root_ts,
                    "when": view.when_ts(root_ts), "author": view.person(None), "state": None,
                    "lane": None, "body": None, "truncated": False, "missing": True}
        else:
            root = dict(_brief(view, root_row, ROOT_CHARS), missing=False)
    others = [item for item in thread if item["ts"] != root_ts and item["id"] != row["id"]]
    context = [dict(_brief(view, item, CONTEXT_CHARS), after=ts_key(item["ts"]) > ts_key(row["ts"]))
               for item in others[-CONTEXT_MESSAGES:]]
    suggestion = None
    if row.get("classifier_suggestion"):
        suggestion = {"lane": row["classifier_suggestion"], "reason": row.get("suggestion_reason"),
                      "version": row.get("classifier_version")}
    return {"id": row["id"], "ts": row["ts"], "when": view.when_ts(row["ts"]),
            "received_at": row.get("received_at"),
            "age_seconds": seconds_since(row.get("received_at"), now),
            "channel": view.channel(row["channel"]), "author": view.person(row.get("author")),
            "attention": row.get("attention"), "origin": row.get("origin"),
            "is_bot": bool(row.get("is_bot")), "has_files": bool(row.get("has_files")),
            "edited": bool(row.get("edited_at")), "followup": bool(row.get("followup")),
            "state": row["state"], "pending_reason": row.get("note"),
            "not_member": not_member(row, membership),
            "suggestion": suggestion, "hints": author_hints(store, config, row, hints),
            "is_reply": is_reply, "body": full.get("body"), "root": root,
            "context": context, "thread_size": len(thread),
            "registry": registry_view(owner, row)}


def queue_page(store, config, view, now, *, offset=0, limit=None, owner=None):
    """human_pending messages, newest first by message time, one page at a time.

    With the P17 owner resolver, `registry_closed` lists every pending item (all pages)
    whose registry row is closed at or after the item's ts, with the staged done note
    `registry:<status>:<registry id>`. Nothing is written here.
    """
    limit = config.console.page_size if limit is None else limit
    rows = store.list_messages(state="human_pending", limit=ALL_ROWS)
    rows.sort(key=lambda row: (ts_key(row["ts"]), row["id"]), reverse=True)
    page = rows[offset:offset + limit]
    hints = {}
    membership = membership_view(store)
    closed = []
    if owner is not None:
        for row in rows:
            info = registry_view(owner, row)
            if info and info["registry_closed"]:
                closed.append({"id": row["id"], "note": info["note"]})
    # P14: every pending id (all pages), so the page can drop staged ids that left the queue.
    return {"total": len(rows), "offset": offset, "limit": limit,
            "pending_ids": [row["id"] for row in rows],
            "registry_closed": closed,
            "items": [queue_item(store, config, view, row, now, hints, membership, owner)
                      for row in page]}


CHANNEL_ID = re.compile(r"[CDG][A-Z0-9]{2,63}")
TS_VALUE = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,9})?")


def owner_lookup(store, config, owner, channel, ts=None):
    """P17 `GET /api/owner`: the owner candidates of a channel or a message.

    `ts` may be a message ts or a thread root ts. A ledger message with that ts supplies
    its thread_ts, so a reply resolves by its thread root. Raises ValueError for an
    invalid channel or ts.
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


def lane_card(store, config, view, resolver, root, lane, now):
    """One lane: session, pause, today's counts, open work and delivery times."""
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
            "inbox_bytes": size}


def lane_cards(store, config, view, resolver, root, now):
    return [lane_card(store, config, view, resolver, root, lane, now) for lane in lane_ids(config)]


def lane_inbox(store, config, view, root, lane, *, full=False, owner=None):
    """The lane inbox as thread groups, produced by the CLI's own `inbox LANE --thread --json`.

    With the P17 owner resolver each group and item carries `registry` badge data."""
    args = argparse.Namespace(lane=lane, include_done=False, thread=True, json=True, full=full,
                              no_body=False, show_body=False,
                              owner=config.always_human.owner_user)
    output = io.StringIO()
    show_inbox(args, root, store, output, view.people, view.zone)
    groups = json.loads(output.getvalue() or "[]")
    for group in groups:
        group["root"]["pending"] = "kind" in group["root"]
        group["root"]["channel_name"] = view.channels.get(group["root"].get("channel"))
        group["root"]["when_full"] = view.when_ts(group["root"].get("ts"))
        root_row = group["root"]
        group["registry"] = registry_view(owner, {"channel": root_row.get("channel"),
                                                  "ts": root_row.get("ts")}) if owner else None
        for item in group["items"]:
            message = store.get_message(item["id"]) or {}
            item["state"] = message.get("state")
            item["lane"] = message.get("lane")
            item["when_full"] = view.when_ts(item.get("ts"))
            item["channel"] = message.get("channel") or root_row.get("channel")
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
        result.append({"id": row["id"], "ts": row["ts"], "when": view.when_ts(row["ts"]),
                       "channel": view.channel(row["channel"]),
                       "author": view.person(row.get("author")), "state": row["state"],
                       "lane": row.get("lane"), "route_reason": row.get("route_reason"),
                       "note": row.get("note"), "suggestion": row.get("classifier_suggestion"),
                       "snippet": snippet, "truncated": truncated})
    return {"query": query, "rows": result, "truncated": len(rows) > limit}


# P13 scope learning: proposals tab and learned scope overrides.

PROPOSAL_ORDER = {"suggested": 0, "open": 1}
LIVE_STATUSES = ("open", "suggested", "confirmed")


def scope_name(view, kind, key):
    """Display name of a scope key: console channel names, people names for persons."""
    return (view.people if kind == "person" else view.channels).get(key)


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
    return [{"channel": row["channel"], "name": view.channels.get(row["channel"]),
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
        notes = [note for note in (view.channels.get(channel), "ledger" if added else None) if note]
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
