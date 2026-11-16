"""Plain-text inbox commands against the shared LedgerStore API."""
import argparse
from collections import Counter
import json
from pathlib import Path
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from slack_dispatch.console import actions
from slack_dispatch.console.actions import inbox_refusal
from slack_dispatch.router.apply import DEFAULT_MIN_EVIDENCE

from .files import append, read, record, validate_lane

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
CUTOVER_NOTE = "seen by the lane before cutover"
# Notes written by the service, the operator or cutover itself; their ack_at and done_at
# are not lane session activity, so lane_last_seen ignores those rows.
SYSTEM_NOTES = ("cutover:", "owner_reaction:", "owner_post", "prehandled", "context",
                "message_deleted", "human:")
# P13 scope: `scope add` and `scope remove` flags, and the key forms each one accepts.
SCOPE_FLAGS = (("channel", "channels"), ("thread", "threads"), ("dm", "dms"), ("person", "persons"))
CONVERSATION = re.compile(r"[CDG][A-Z0-9]{2,}")
PERSON = re.compile(r"[UW][A-Z0-9]{2,}")
SLACK_TS = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,9})?")
SCOPE_PREFIXES = {"channel": "CG", "dm": "DCG"}
# Harvest proposal YAML sections read by `scope import`.
PROPOSAL_SECTIONS = (("channel_defaults_add", "channel"), ("channel_defaults_change", "channel"),
                     ("dm_defaults_add", "dm"), ("dm_defaults_change", "dm"),
                     ("person_rules_add", "person"), ("person_lane_candidates", "person"))
BINDING_SECTIONS = (("thread_bindings_add", False), ("thread_bindings_rebind", True))
LIVE_PROPOSALS = ("open", "suggested", "confirmed")
REPAIR_SINCE_DAYS = 30
# P18 `deliveries`: default window in hours (0 = every row) and the clock (tests patch it).
DELIVERY_HOURS = 48
DELIVERY_CLOCK = time.time
# P19 `undo`: the clock of the undo window (tests patch it).
UNDO_CLOCK = time.time
BOT_NOTE = "excluded:bot"


class BadInput(ValueError):
    pass


class AmbiguousLane(BadInput):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise BadInput("Invalid command arguments")


def build_parser():
    parser = Parser(description="Lane inbox CLI")

    def options(target, defaults=False):
        fallback = None if defaults else argparse.SUPPRESS
        target.add_argument("--ledger", default=fallback)
        target.add_argument("--inbox-root", default=fallback)
        # --config is accepted before and after the subcommand; SLACK_DISPATCH_CONFIG is the fallback.
        target.add_argument("--config", default=fallback)
        target.add_argument("--show-body", action="store_true", default=False if defaults else argparse.SUPPRESS)

    options(parser, True)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("inbox", "ack", "done", "reroute", "claim", "queue", "assign"):
        sub = commands.add_parser(command)
        options(sub)
        if command in ("inbox", "claim"):
            sub.add_argument("lane")
        elif command in ("ack", "done"):
            sub.add_argument("ids", nargs="*")
            sub.add_argument("--thread", metavar="ROOT_ID")
            sub.add_argument("--lane")
        elif command != "queue":
            sub.add_argument("id")
        if command in ("done", "assign"):
            sub.add_argument("--note")
        if command == "reroute":
            sub.add_argument("--to", required=True, dest="lane")
        if command == "assign":
            sub.add_argument("lane")
        if command == "claim":
            sub.add_argument("--session")
        if command == "inbox":
            sub.add_argument("--include-done", action="store_true")
            sub.add_argument("--thread", action="store_true")
            sub.add_argument("--json", action="store_true")
            sub.add_argument("--full", action="store_true")
            sub.add_argument("--no-body", action="store_true")
            sub.add_argument("--owner")
            # P20: a `link` column with the Slack permalink (console.workspace_domain).
            sub.add_argument("--links", action="store_true")
    sub = commands.add_parser("import-bindings")
    options(sub)
    sub.add_argument("path")
    sub.add_argument("--replace", action="store_true")
    sub = commands.add_parser("cutover")
    options(sub)
    sub.add_argument("lane")
    sub.add_argument("--seen-before", metavar="TS")
    sub.add_argument("--note")
    sub.add_argument("--apply", action="store_true")
    # P13: lane scope registration and recorded sends.
    sub = commands.add_parser("scope")
    options(sub)
    scope = sub.add_subparsers(dest="scope_command", required=True)
    target = scope.add_parser("add")
    options(target)
    target.add_argument("lane")
    for flag, dest in SCOPE_FLAGS:
        target.add_argument("--" + flag, action="append", default=[], dest=dest)
    target.add_argument("--note")
    target = scope.add_parser("list")
    options(target)
    target.add_argument("lane", nargs="?")
    target = scope.add_parser("remove")
    options(target)
    target.add_argument("lane")
    group = target.add_mutually_exclusive_group(required=True)
    for flag, _ in SCOPE_FLAGS:
        group.add_argument("--" + flag, dest="remove_" + flag)
    target = scope.add_parser("import")
    options(target)
    target.add_argument("path")
    target.add_argument("--dry-run", action="store_true")
    # P16: repair rows flagged as bot posts that a human wrote through an app.
    sub = commands.add_parser("repair")
    options(sub)
    repair = sub.add_subparsers(dest="repair_command", required=True)
    target = repair.add_parser("bot-flag")
    options(target)
    target.add_argument("--apply", action="store_true")
    target.add_argument("--since-days", type=int, default=REPAIR_SINCE_DAYS)
    target.add_argument("--assume-human", action="store_true")
    target.add_argument("--json", action="store_true")
    sub = commands.add_parser("sent")
    options(sub)
    sub.add_argument("channel")
    sub.add_argument("ts")
    sub.add_argument("--lane", required=True)
    sub.add_argument("--thread", dest="root_ts", metavar="ROOT_TS")
    sub.add_argument("--kind", choices=("reply", "root", "dm"))
    # P17: owner lookup and registry sync (read the registry named by owner_resolver).
    sub = commands.add_parser("owner")
    options(sub)
    sub.add_argument("channel", nargs="?")
    sub.add_argument("--ts", dest="owner_ts", metavar="TS")
    sub.add_argument("--pending", action="store_true")
    sub.add_argument("--apply", action="store_true")
    sub.add_argument("--json", action="store_true")
    sub = commands.add_parser("registry")
    options(sub)
    registry = sub.add_subparsers(dest="registry_command", required=True)
    target = registry.add_parser("check")
    options(target)
    target.add_argument("--lane")
    target.add_argument("--json", action="store_true")
    # P18: the delivery log (what the dispatcher sent where, and whether it was received).
    sub = commands.add_parser("deliveries")
    options(sub)
    sub.add_argument("--lane")
    sub.add_argument("--hours", type=int, default=DELIVERY_HOURS)
    sub.add_argument("--status")
    sub.add_argument("--json", action="store_true")
    # P20: operator backfill of display names (users.info for ledger authors).
    sub = commands.add_parser("directory")
    options(sub)
    directory = sub.add_subparsers(dest="directory_command", required=True)
    target = directory.add_parser("refresh")
    options(target)
    target.add_argument("--missing-only", action="store_true")
    target.add_argument("--json", action="store_true")
    # P19: undo a recent console action (same rules as the console).
    sub = commands.add_parser("undo")
    options(sub)
    target = sub.add_mutually_exclusive_group(required=True)
    target.add_argument("--last", action="store_true")
    target.add_argument("action_id", nargs="?", type=int)
    sub.add_argument("--json", action="store_true")
    return parser


def age(row):
    try:
        then = datetime.fromisoformat(row["delivered_at"])
        return str(max(0, int((datetime.now(timezone.utc) - then).total_seconds()))) + "s"
    except (KeyError, ValueError, TypeError):
        return "unknown"


def use_utf8(stream):
    """Reconfigure a console stream to UTF-8 so smart quotes survive a legacy code page."""
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def display_zone(name, stderr):
    """Resolve display_timezone; UTC needs no time zone database."""
    if not name or name == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
        print("warning: display_timezone is not available on this host (install tzdata); "
              "showing UTC", file=stderr)
        return timezone.utc


def display_settings(config_path, stderr):
    """Return (people, zone) from an optional config file; defaults are raw ids and UTC."""
    people, zone, _ = display_config(config_path, stderr)
    return people, zone


def display_config(config_path, stderr):
    """Return (people, zone, config) from an optional config file (P20 keeps the config for
    channel names and `console.workspace_domain`); without a file ({}, UTC, None)."""
    if not config_path:
        return {}, timezone.utc, None
    from slack_dispatch.config.loader import load_config
    config = load_config(config_path)
    return dict(config.people), display_zone(config.display_timezone, stderr), config


def display_names(config, store):
    """P20: the shared resolver (people, then the ledger directory, then the id), one per
    command. Works without a config: the ledger tables still supply names."""
    from slack_dispatch.console.queries import DisplayNames
    return DisplayNames(config, store)


def permalink_for(config, channel, ts, thread_ts=None):
    """P20: the message permalink (`console.workspace_domain`), or None."""
    from slack_dispatch.console.queries import message_permalink, workspace_domain
    return message_permalink(workspace_domain(config), channel, ts, thread_ts)


def when(ts, zone):
    """Message time as MM-DD HH:MM in the display zone, or - when the ts is unusable."""
    try:
        moment = EPOCH + timedelta(seconds=float(Decimal(str(ts))))
        return moment.astimezone(zone).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, ArithmeticError, OverflowError):
        return "-"


def delivery_key(row):
    try:
        return datetime.fromisoformat(row["delivered_at"]).timestamp()
    except (KeyError, ValueError, TypeError):
        return float("inf")


def thread_id(row):
    return f"slack:{row['channel']}:{row.get('thread_ts') or row['ts']}"


def is_deleted(message):
    """A message_deleted event marked the row (deleted_at, or the note of older rows)."""
    return bool(message.get("deleted_at")) or message.get("note") == "message_deleted"


def inbox_rows(root, lane, store, include_done=False):
    """Join raw records ourselves to retain historical followup flags."""
    grouped = {}
    for record_row in read(root, lane, include_done=True):
        mid = record_row["id"]
        if mid not in grouped:
            grouped[mid] = dict(record_row, kind="new", edited="no")
        row = grouped[mid]
        if delivery_key(record_row) < delivery_key(row):
            row["delivered_at"] = record_row["delivered_at"]
        if record_row.get("kind") != "followup":
            row["kind"] = record_row.get("kind", "new")
        # Edited means edited: a followup-kind record (message_changed) or ledger edited_at.
        # The followup flag alone (a recent reply in the same thread) does not count.
        if record_row.get("kind") == "followup":
            row["edited"] = "yes"
    result = []
    for mid, row in grouped.items():
        message = store.get_message(mid) or {}
        if message.get("lane") not in (None, lane):
            continue
        # P19 fix1: a row without a lane that waits in the human queue or was excluded is
        # nobody's lane item (for example an assign that the console undid).
        if message and message.get("lane") is None and message.get("state") in ("human_pending",
                                                                                "excluded"):
            continue
        # Context rows and owner posts are never inbox items (only roots or owner markers).
        if message and inbox_refusal(dict(message, id=mid)):
            continue
        row["state"] = message.get("state") or "unknown"
        if is_deleted(message):
            # Deleted messages are hidden; --include-done shows them with a `deleted` marker.
            if not include_done:
                continue
            row["state"] = "deleted"
        if not include_done and row["state"] == "done":
            continue
        for key in ("channel", "ts", "thread_ts"):
            if key in message:
                row[key] = message[key]
        if message.get("edited_at"):
            row["edited"] = "yes"
        row["attention"] = message.get("attention") or "-"
        row["author"] = message.get("author") or "-"
        row["thread"] = thread_id(row) if row.get("thread_ts") else "-"
        row["age"] = age(row)
        result.append(row)
    return sorted(result, key=delivery_key)


def stored_body(store, mid):
    message = store.get_message(mid, include_body=True) or {}
    if is_deleted(message):
        return "[deleted]"
    body = message.get("body", message.get("text"))
    return "[no body stored]" if body is None else body


def all_messages(store):
    # The public API has neither an offset nor an unbounded sentinel.
    limit = 100
    while True:
        rows = store.list_messages(limit=limit)
        if len(rows) < limit:
            return rows
        limit *= 2


LIST_COLUMNS = ("id", "age", "when", "channel", "thread", "kind", "edited", "attention", "author",
                "state")
ITEM_COLUMNS = ("id", "author", "ts", "when", "kind", "edited", "attention")
# P20 fix1: text output appends the resolved names (spaces become `_`); JSON keeps ids.
NAME_COLUMNS = ("channel_name", "author_name")


def text_label(value):
    """A name for space-separated text output: spaces become `_` (JSON keeps the original)."""
    return "-" if not value else str(value).replace(" ", "_")


# JSON rows also carry the raw message ts and the raw author id.
JSON_EXTRA = ("ts", "author_id")


def show_inbox(args, root, store, stdout, people=None, zone=timezone.utc, names=None,
               links=None):
    """`inbox LANE` as text or JSON.

    P20: `names` (a DisplayNames; else the `people` map) resolves authors and, in text,
    channels; JSON keeps channel ids and adds `channel_name` when a name is known. `links(channel, ts,
    thread_ts)` returns a permalink: JSON rows then carry `permalink`, and `--links` adds a
    text `link` column (`-` without a workspace domain).
    """
    people = people or {}
    with_links = bool(getattr(args, "links", False))

    def author_name(author):
        if names is not None:
            return names.user(author) if author and author != "-" else author
        return people.get(author, author)

    def channel_label(channel):
        return names.channel(channel) if names is not None and channel else channel

    def legacy_author(author):
        # fix1: the text `author` column keeps its pre-P20 value (config people, else id).
        return people.get(author, author)

    def link(channel, ts, thread_ts=None):
        return links(channel, ts, thread_ts) if links is not None else None

    rows = inbox_rows(root, args.lane, store, args.include_done)
    for row in rows:
        row["when"] = when(row.get("ts"), zone)
        row["author_id"] = row["author"]
        row["author"] = author_name(row["author"])
        row["permalink"] = link(row.get("channel"), row.get("ts"), row.get("thread_ts"))
    if not args.thread:
        output = [{key: row[key] for key in LIST_COLUMNS + (JSON_EXTRA if args.json else ())}
                  for row in rows]
        for row, source in zip(output, rows):
            if args.json:
                channel_name = names.channel_name(source.get("channel")) if names else None
                if channel_name:
                    row["channel_name"] = channel_name
                if links is not None:
                    row["permalink"] = source["permalink"]
            else:
                # fix1: existing columns unchanged (channel id); names go to added columns.
                row["author"] = legacy_author(source["author_id"])
                row["channel_name"] = text_label(channel_label(source.get("channel")))
                row["author_name"] = text_label(author_name(source["author_id"]))
                if with_links:
                    row["link"] = source["permalink"] or "-"
        if args.show_body and not args.no_body:
            for row in output:
                row["body"] = stored_body(store, row["id"])
        if args.json:
            print(json.dumps(output, ensure_ascii=False), file=stdout)
        else:
            columns = LIST_COLUMNS + NAME_COLUMNS + (("link",) if with_links else ())
            print(" ".join(columns), file=stdout)
            for row in output:
                print(*(row[key] for key in columns), file=stdout)
                if "body" in row:
                    print(row["id"], row["body"], file=stdout)
        return
    grouped = {}
    for row in rows:
        grouped.setdefault(thread_id(row), []).append(row)
    ledger_rows = all_messages(store) if grouped else []
    output = []
    for root_id, pending in grouped.items():
        pending.sort(key=lambda row: Decimal(row["ts"]))
        message = store.get_message(root_id)
        author = (message or {}).get("author") or "-"
        root_ts = pending[0].get("thread_ts") or pending[0]["ts"]
        root_row = {"id": root_id, "channel": pending[0]["channel"], "ts": root_ts,
                    "when": when(root_ts, zone), "author": author_name(author),
                    "author_id": author,
                    "state": ("deleted" if message and is_deleted(message)
                              else (message or {}).get("state") or "unknown"),
                    "missing": message is None}
        for row in pending:
            if row["id"] == root_id:
                root_row.update({key: row[key] for key in ("kind", "edited", "attention")})
        if not args.no_body:
            body = stored_body(store, root_id)
            root_row["body"] = body if args.full else body[:400]
        items = []
        for row in pending:
            if row["id"] == root_id:
                continue
            item = {key: row[key] for key in ITEM_COLUMNS + (("author_id",) if args.json else ())}
            if links is not None:
                item["permalink"] = row["permalink"]
            if not args.no_body:
                item["body"] = stored_body(store, row["id"])
            items.append(item)
        channel_name = names.channel_name(root_row["channel"]) if names else None
        if channel_name:
            root_row["channel_name"] = channel_name
        if links is not None:
            root_row["permalink"] = link(root_row["channel"], root_ts)
        group = {"root": root_row, "items": items}
        replies = [row for row in ledger_rows if thread_id(row) == root_id
                   and Decimal(row["ts"]) > Decimal(pending[0]["ts"])
                   and (row.get("route_reason") == "owner_post"
                        or (args.owner and row.get("author") == args.owner))]
        if replies:
            group["owner_replied_at"] = max(replies, key=lambda row: Decimal(row["ts"]))["ts"]
        output.append(group)
    if args.json:
        print(json.dumps(output, ensure_ascii=False), file=stdout)
        return
    item_ids = {row["id"]: row["author_id"] for row in rows}
    for group in output:
        row = group["root"]
        print(row["id"], "channel", row["channel"], "author", legacy_author(row["author_id"]),
              "ts", row["ts"], "when", row["when"], "state", row["state"],
              "root not in ledger" if row["missing"] else "root",
              "channel_name", text_label(channel_label(row["channel"])),
              "author_name", text_label(row["author"]),
              *(("link", row.get("permalink") or "-") if with_links else ()), file=stdout)
        if "kind" in row:
            print("kind", row["kind"], "edited", row["edited"], "attention", row["attention"], file=stdout)
        if "body" in row:
            print(row["body"], file=stdout)
        for item in group["items"]:
            print(*(legacy_author(item_ids[item["id"]]) if key == "author" else item[key]
                    for key in ITEM_COLUMNS), text_label(item["author"]),
                  *((item.get("permalink") or "-",) if with_links else ()), file=stdout)
            if "body" in item:
                print(item["body"], file=stdout)
        if "owner_replied_at" in group:
            print("owner replied at", group["owner_replied_at"], file=stdout)


def thread_pending(root, lane, store, thread):
    return [row["id"] for row in inbox_rows(root, lane, store) if thread_id(row) == thread]


def pending_lanes(root, store, thread, skip=None):
    """Map each known lane whose inbox file holds pending items of the thread to their ids."""
    found = {}
    for lane_row in store.list_lanes():
        lane = lane_row.get("id")
        if lane == skip:
            continue
        try:
            validate_lane(lane)
        except ValueError:
            continue
        ids = thread_pending(root, lane, store, thread)
        if ids:
            found[lane] = ids
    return found


def bulk_ids(args, root, store, stderr):
    if not args.thread:
        return list(dict.fromkeys(args.ids))
    parts = args.thread.split(":")
    if len(parts) != 3 or parts[0] != "slack" or not all(parts):
        raise BadInput("Invalid root id")
    try:
        stamp = Decimal(parts[2])
        if not stamp.is_finite() or stamp < 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        raise BadInput("Invalid root timestamp") from None
    root_message = store.get_message(args.thread)
    if root_message and thread_id(root_message) != args.thread:
        raise BadInput("Expected a thread root")
    lane = args.lane or (root_message or {}).get("lane")
    ids = []
    if lane:
        validate_lane(lane)
        ids = thread_pending(root, lane, store, args.thread)
    if not ids and not args.lane:
        # Replies may sit in another lane (reroute) or the root may be a lane-less context row.
        found = pending_lanes(root, store, args.thread, skip=lane)
        if len(found) > 1:
            raise AmbiguousLane(f"ambiguous lane for thread {args.thread}: "
                                f"{', '.join(sorted(found))} (use --lane)")
        if found:
            lane, ids = next(iter(found.items()))
    if not ids:
        if root_message is None:
            return None
        where = f"lane {lane}" if lane else "any lane"
        print(f"no pending items for thread {args.thread} in {where}", file=stderr)
    return ids


def epoch(value):
    """Epoch seconds from a Slack ts, epoch number or ISO time (naive = local); None if unusable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        number = None
    if number is not None:
        return float(number) if number.is_finite() else None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.timestamp()


def lane_last_seen(rows):
    """Latest ack_at or done_at of the lane's own work; system-closed rows do not count."""
    latest = None
    for row in rows:
        if str(row.get("note") or "").startswith(SYSTEM_NOTES):
            continue
        for key in ("ack_at", "done_at"):
            value = epoch(row.get(key))
            if value is not None and (latest is None or value > latest):
                latest = value
    return latest


def delivered_epoch(store, row, lane):
    """When the item reached the lane: the latest of received_at, assigned_at and the
    service_deliveries time of its thread and lane (later signals keep items open)."""
    thread = getattr(store, "thread_delivery", None)
    signals = [epoch(row.get("received_at")), epoch(row.get("assigned_at"))]
    if callable(thread):
        signals.append(epoch(thread(row["source"], row["channel"],
                                    row.get("thread_ts") or row["ts"], lane)))
    known = [value for value in signals if value is not None]
    return max(known) if known else None


def stamp(value):
    if value is None:
        return "none"
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def unconsumed_backfill(root, lane, store, ids):
    """Ids (of `ids`) with a `backfill` record in the lane inbox that is still unconsumed:
    the message is open, owned by the lane (or by none) and was not done after the
    record was written. Acked items stay pending until done, as in the inbox view.

    The service's own first delivery of a reconcile message is also kind `backfill`; it is
    not a cutover redelivery and does not count, so a first cutover still delivers it.
    """
    found = set()
    if not ids:
        return found
    first = {}
    for row in read(root, lane, include_done=True):
        if row.get("id") not in ids:
            continue
        initial = first.setdefault(row["id"], row) is row
        if row.get("kind") != "backfill" or row["id"] in found:
            continue
        message = store.get_message(row["id"]) or {}
        if initial and message.get("origin") == "reconcile":
            continue
        if message.get("state") == "done" or message.get("lane") not in (None, lane):
            continue
        done, written = epoch(message.get("done_at")), epoch(row.get("delivered_at"))
        if done is not None and written is not None and done >= written:
            continue
        found.add(row["id"])
    return found


def cutover(args, root, store, stdout, stderr):
    """Close only what the lane demonstrably saw; re-deliver the rest as backfill."""
    if store.get_lane(args.lane) is None:
        print("Lane not found", file=stderr)
        return 3
    override = None
    if args.seen_before is not None:
        override = epoch(args.seen_before)
        if override is None:
            raise BadInput("Invalid --seen-before")
    rows = store.lane_messages(args.lane)
    last_seen = override if override is not None else lane_last_seen(rows)
    close, keep, refused = [], [], []
    for row in rows:
        if row["state"] not in ("assigned", "acked"):
            continue
        refusal = inbox_refusal(row)
        if refusal:
            refused.append(refusal)
            continue
        delivered = delivered_epoch(store, row, args.lane)
        if (not row.get("attention") and last_seen is not None and delivered is not None
                and delivered < last_seen):
            close.append(row)
        else:
            keep.append(row)
    attention = sum(bool(row.get("attention")) for row in keep)
    # P13: a rerun must not append the same open items again; an unconsumed backfill
    # record of the lane already carries them.
    pending = unconsumed_backfill(root, args.lane, store, {row["id"] for row in keep})
    deliver = [row for row in keep if row["id"] not in pending]
    redelivered = 0
    if args.apply:
        note = "cutover: " + (args.note or CUTOVER_NOTE)
        for row in close:
            store.set_state(row["id"], "done", note=note)
        records = []
        for row in deliver:
            message = store.get_message(row["id"]) or row
            records.append(record(dict(message, id=row["id"]), "backfill"))
        if records:
            append(root, args.lane, records)
        redelivered = len(records)
    mode = "apply" if args.apply else "dry-run"
    basis = "seen-before" if override is not None else "lane activity"
    print(f"cutover {args.lane} mode={mode} lane_last_seen={stamp(last_seen)} basis={basis}",
          file=stdout)
    print(f"closed={len(close)} kept_open={len(keep)} "
          f"redelivered={redelivered if args.apply else len(deliver)} attention_kept={attention} "
          f"refused={len(refused)}", file=stdout)
    if pending:
        print(f"already_backfilled={len(pending)} (unconsumed backfill record in the inbox; "
              "not appended again)", file=stdout)
    for row in keep:
        print(row["id"], "kept_open", "attention=" + (row.get("attention") or "-"), file=stdout)
    if not args.apply:
        print("dry run: nothing changed; rerun with --apply", file=stdout)
    for line in refused:
        print(line, file=stderr)
    return 2 if refused else 0


class RepairDelivery:
    """Delivery for `repair bot-flag`: the mechanisms `assign` and `cutover` use, offline.

    Lane items get a `backfill` record in the lane inbox (no wake, like cutover). Human
    notices are queued as durable `human_notices` rows that the running service flushes
    (immediate rows on its next flush), and the human queue export is rewritten.
    """

    def __init__(self, config, store, root, clock=None):
        import time
        self.config, self.store, self.root = config, store, root
        self.clock = clock or time.time

    def inbox(self, lane, message):
        append(self.root, lane, [record(dict(message), "backfill")])

    def human(self, message_id, kind, immediate):
        from slack_dispatch.pipeline import queue_human_notice
        queue_human_notice(self.config, self.store, message_id, kind, immediate=immediate,
                           now=self.clock())

    def export(self):
        from slack_dispatch.queue.human import export_jsonl
        path = Path(self.config.service.state_dir) / "human_queue.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            export_jsonl(self.store, path)
        except Exception:
            return False
        return True


def repair_plan(row, config, store, assume_human):
    """The planned action for one `is_bot = 1` row with an author, or None when the author
    is still a bot (a known bot user or a `user_directory` row with is_bot). The CLI stays
    offline: only the ledger table and the config are read."""
    author = row.get("author")
    if not author or author in config.bot_messages.known_bot_users:
        return None
    lookup = getattr(store, "get_user_directory", None)
    entry = lookup(author) if callable(lookup) else None
    if entry is not None and entry.get("is_bot"):
        return None
    # Context rows and owner posts are never inbox items (the assign and cutover rule).
    refusal = inbox_refusal(row)
    if refusal:
        return "refused:context" if "context row" in refusal else "refused:owner_post"
    if author == config.always_human.owner_user:
        return "refused:owner"
    if entry is None and not assume_human:
        return "skip:unknown"
    if row.get("state") == "excluded" and row.get("note") == BOT_NOTE:
        return "reroute"
    if row.get("state") in ("assigned", "acked"):
        return "attention"
    return "fix"


def repair_row(row, plan, config, store, delivery):
    """Apply one planned repair; returns the result word.

    The flag is cleared last, after delivery, so a failed inbox append leaves the row as
    it was (still `is_bot = 1`, still excluded) and a rerun retries it.
    """
    from slack_dispatch.pipeline import routing_config
    from slack_dispatch.router.apply import apply_decision
    from slack_dispatch.router.engine import attention_for, route
    if plan.startswith("refused:"):
        return plan
    if plan == "skip:unknown":
        return "skipped:unknown"
    mid = row["id"]
    if plan == "fix":
        store.set_is_bot(mid, False)
        return "fixed"
    full = store.get_message(mid, include_body=True) or dict(row)
    message = dict(full, id=mid, is_bot=0)
    message["text"] = full.get("text") if "text" in full else full.get("body")
    immediate = config.human_queue.immediate_flags
    if plan == "attention":
        flag = attention_for(message, config)
        result = "fixed"
        if flag and not row.get("attention"):
            store.set_attention(mid, flag)
            delivery.human(mid, "attention:" + flag, flag in immediate)
            result = "attention:" + flag
        store.set_is_bot(mid, False)
        return result
    # Route again as if newly received (no classifier: the CLI stays offline).
    decision = route(message, routing_config(config, store), store.lookup_binding)
    if decision.lane is not None and decision.rule != "excluded":
        # Deliver first: a failed append changes nothing.
        delivery.inbox(decision.lane, message)
    outcome = apply_decision(store, mid, decision, None)
    if outcome.state == "assigned":
        key = (message["source"], message["channel"], message.get("thread_ts") or message["ts"])
        if store.lookup_binding(*key) is None:
            store.bind_thread(*key, outcome.lane)
        if decision.attention:
            delivery.human(mid, "attention:" + decision.attention,
                           decision.attention in immediate)
        result = "rerouted:" + outcome.lane
    elif outcome.state == "human_pending":
        flag = decision.attention
        delivery.human(mid, "attention:" + flag if flag else "message", flag in immediate)
        delivery.export()
        result = "rerouted:human"
    else:
        result = "rerouted:" + outcome.state
    store.set_is_bot(mid, False)
    return result


def repair_bot_flag(store, config, *, apply=False, since_days=REPAIR_SINCE_DAYS,
                    assume_human=False, delivery=None, now=None):
    """`repair bot-flag`: rows the old `bot_id` rule flagged although a human wrote them.

    Returns one dict per listed row (id, author, state, lane, note, action and, with
    apply, result). Rows whose author is still a bot are not listed.
    """
    now = now or datetime.now().astimezone()
    since = (now - timedelta(days=since_days)).isoformat()
    rows = []
    for row in store.bot_flagged_messages(since=since):
        plan = repair_plan(row, config, store, assume_human)
        if plan is None:
            continue
        item = {key: row.get(key) for key in ("id", "author", "state", "lane", "note")}
        item["action"] = plan
        if apply:
            try:
                item["result"] = repair_row(row, plan, config, store, delivery)
            except Exception as error:
                item["result"] = "error"
                item["error_class"] = type(error).__name__
        rows.append(item)
    return rows


def repair_command(args, root, store, stdout, stderr, *, delivery=None, now=None):
    """CLI `repair bot-flag [--apply] [--since-days N] [--assume-human] [--json]`."""
    if args.since_days <= 0:
        raise BadInput("Invalid --since-days")
    config = optional_config(args)
    if config is None:
        print("Config is required (--config or SLACK_DISPATCH_CONFIG)", file=stderr)
        return 2
    if delivery is None:
        delivery = RepairDelivery(config, store, root)
    rows = repair_bot_flag(store, config, apply=args.apply, since_days=args.since_days,
                           assume_human=args.assume_human, delivery=delivery, now=now)
    mode = "apply" if args.apply else "dry-run"
    counts = dict(sorted(Counter(row.get("result", row["action"]) for row in rows).items()))
    if args.json:
        print(json.dumps({"mode": mode, "since_days": args.since_days, "rows": rows,
                          "counts": counts}, ensure_ascii=False), file=stdout)
    else:
        print(f"repair bot-flag mode={mode} since_days={args.since_days} rows={len(rows)}",
              file=stdout)
        for row in rows:
            print(" ".join(str(row.get(key) or "-") for key in
                           ("id", "author", "state", "lane", "note")),
                  row.get("result", row["action"]), file=stdout)
        if counts:
            print(" ".join(f"{key}={value}" for key, value in counts.items()), file=stdout)
        if not args.apply:
            print("dry run: nothing changed; rerun with --apply", file=stdout)
    return 1 if any(row.get("result") == "error" for row in rows) else 0


def claimant():
    """Who records a scope claim: SLACK_DISPATCH_SESSION, CLAUDE_SESSION_ID, else `cli`."""
    return os.environ.get("SLACK_DISPATCH_SESSION") or os.environ.get("CLAUDE_SESSION_ID") or "cli"


def optional_config(args, *, strict=True):
    """The config from --config or SLACK_DISPATCH_CONFIG, else None. An unreadable config
    raises (exit 2) when strict, and is ignored otherwise."""
    path = args.config or os.environ.get("SLACK_DISPATCH_CONFIG")
    if not path:
        return None
    from slack_dispatch.config.loader import load_config
    try:
        return load_config(path)
    except ValueError:
        if strict:
            raise
        return None


def min_evidence(config):
    return config.scope_learning.min_evidence if config is not None else DEFAULT_MIN_EVIDENCE


def scope_key(kind, value):
    """Validate a claim key; thread keys are `CHANNEL:TS` (a `slack:` prefix is accepted)."""
    if not isinstance(value, str):
        raise BadInput("Invalid scope key")
    if kind == "thread":
        parts = value.split(":")
        if len(parts) == 3 and parts[0] == "slack":
            parts = parts[1:]
        if (len(parts) != 2 or not CONVERSATION.fullmatch(parts[0])
                or not SLACK_TS.fullmatch(parts[1])):
            raise BadInput("Invalid thread key")
        return parts[0] + ":" + parts[1]
    if kind == "person":
        if not PERSON.fullmatch(value):
            raise BadInput("Invalid person id")
        return value
    if not CONVERSATION.fullmatch(value) or value[0] not in SCOPE_PREFIXES[kind]:
        raise BadInput("Invalid conversation id")
    return value


def auto_default(store, config, channel, lane):
    """scope:auto applies a channel claim as the channel default only when the channel has
    no default (config or ledger) and no other lane ever claimed or was learned for it."""
    if config is None or channel in config.excluded_channels:
        return False
    if channel in config.channel_defaults or channel in config.dm_defaults:
        return False
    if store.lookup_scope_claim("channel", channel) or store.lookup_scope_claim("dm", channel):
        return False
    if store.scope_claim_lanes("channel", channel) != {lane}:
        return False
    return not any(row["kind"] == "channel" and row["key"] == channel and row["lane"] != lane
                   for row in store.list_scope_proposals(LIVE_PROPOSALS))


def scope_add(args, store, config, stdout, stderr):
    claims = [(kind, scope_key(kind, value)) for kind, dest in SCOPE_FLAGS
              for value in getattr(args, dest)]
    if not claims:
        raise BadInput("Supply at least one claim")
    if store.get_lane(args.lane) is None:
        print("Lane not found", file=stderr)
        return 3
    lane, by, threshold = args.lane, claimant(), min_evidence(config)
    if config is None and any(kind == "channel" for kind, _ in claims):
        print("note: without --config scope:auto is not applied; channel claims stay proposals",
              file=stderr)
    for kind, key in dict.fromkeys(claims):
        if kind == "thread":
            channel, ts = key.split(":")
            previous = store.lookup_binding("slack", channel, ts)
            store.bind_thread("slack", channel, ts, lane)
            store.add_scope_claim(lane, "thread", key, note=args.note, claimed_by=by,
                                  confirmed_by="binding")
            moved = f" (was {previous})" if previous not in (None, lane) else ""
            print(f"thread {key} {lane} bound{moved}", file=stdout)
            continue
        created = store.add_scope_claim(lane, kind, key, note=args.note, claimed_by=by)
        if created:
            store.record_scope_evidence(kind, key, lane, "scope", min_evidence=threshold)
        status = "claimed" if created else "refreshed"
        if kind == "channel" and auto_default(store, config, key, lane):
            store.confirm_scope_claim(lane, "channel", key, by="scope:auto")
            status = "auto-applied (scope:auto)"
        print(f"{kind} {key} {lane} {status}", file=stdout)
    return 0


def scope_list(args, store, stdout):
    print("lane kind key status confirmed_by claimed_by claimed_at note", file=stdout)
    for row in store.list_scope_claims(args.lane):
        print(row["lane"], row["kind"], row["key"], row["status"], row["confirmed_by"] or "-",
              row["claimed_by"], row["claimed_at"], row["note"] or "-", file=stdout)
    return 0


def scope_remove(args, store, stdout, stderr):
    kind, value = next((kind, getattr(args, "remove_" + kind)) for kind, _ in SCOPE_FLAGS
                       if getattr(args, "remove_" + kind) is not None)
    key = scope_key(kind, value)
    if not store.remove_scope_claim(args.lane, kind, key):
        print("Claim not found", file=stderr)
        return 3
    print(f"{kind} {key} {args.lane} removed", file=stdout)
    return 0


def harvest_lane(entry):
    """Lane of a harvest entry: `{default: LANE}`, `{lane: LANE}`, a bare lane or null."""
    lane = entry.get("default", entry.get("lane")) if isinstance(entry, dict) else entry
    if lane is not None:
        validate_lane(lane)
    return lane


def parse_harvest(path):
    """Validate the whole harvest proposal YAML before anything is written."""
    import yaml
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise BadInput("Cannot read the proposal file") from None
    data = {} if data is None else data
    if not isinstance(data, dict):
        raise BadInput("The proposal file must be a mapping")
    proposals, bindings = [], []
    for section, kind in PROPOSAL_SECTIONS:
        value = data.get(section) or {}
        if isinstance(value, list):
            items = []
            for row in value:
                if not isinstance(row, dict):
                    raise BadInput("Invalid proposal row")
                items.append((row.get("person", row.get("id")), row))
        elif isinstance(value, dict):
            items = list(value.items())
        else:
            raise BadInput("Invalid proposal section")
        for key, entry in items:
            proposals.append((kind, scope_key(kind, key), harvest_lane(entry)))
    for section, replace in BINDING_SECTIONS:
        value = data.get(section) or []
        if not isinstance(value, list):
            raise BadInput("Invalid binding section")
        for row in value:
            if not isinstance(row, dict) or any(
                    not isinstance(row.get(key), str) or not row[key].strip()
                    for key in ("source", "channel", "thread_ts", "lane")):
                raise BadInput("Invalid binding row")
            scope_key("thread", row["channel"] + ":" + row["thread_ts"])
            validate_lane(row["lane"])
            bindings.append((row, replace))
    conflicts = data.get("conflicts") or []
    unused = data.get("unused_in_config") or []
    if not isinstance(conflicts, list) or not isinstance(unused, list):
        raise BadInput("Invalid conflicts section")
    return proposals, bindings, conflicts, unused


def scope_import(args, store, config, stdout):
    proposals, bindings, conflicts, unused = parse_harvest(args.path)
    write, threshold = not args.dry_run, min_evidence(config)
    counts, unknown, keys = Counter(), set(), {}
    for kind, key, lane in proposals:
        if lane is None:
            counts["skipped_null"] += 1
            continue
        if store.get_lane(lane) is None:
            counts["proposal_unknown"] += 1
            unknown.add(lane)
            continue
        keys.setdefault((kind, key), set()).add(lane)
        if write:
            result = store.import_scope_proposal(kind, key, lane, min_evidence=threshold)
        else:
            row = store.get_scope_proposal(kind, key, lane)
            kept = row is not None and (row["imported"] or row["status"] in ("confirmed", "rejected"))
            result = "kept" if kept else "added"
        counts["proposal_" + result] += 1
    for row, replace in bindings:
        lane = row["lane"]
        if store.get_lane(lane) is None:
            counts["binding_unknown"] += 1
            unknown.add(lane)
            continue
        key = (row["source"], row["channel"], row["thread_ts"])
        current = store.lookup_binding(*key)
        if current == lane or (current is not None and not replace):
            counts["binding_kept"] += 1
            continue
        if write:
            store.bind_thread(*key, lane)
        counts["binding_added" if current is None else "binding_rebound"] += 1
    print(f"proposals added={counts['proposal_added']} kept={counts['proposal_kept']} "
          f"skipped_null={counts['skipped_null']} skipped_unknown_lane={counts['proposal_unknown']}",
          file=stdout)
    print(f"bindings added={counts['binding_added']} rebound={counts['binding_rebound']} "
          f"kept={counts['binding_kept']} skipped_unknown_lane={counts['binding_unknown']}", file=stdout)
    if unknown:
        print("Unknown lanes: " + ", ".join(sorted(unknown)), file=stdout)
    live = store.list_scope_proposals(LIVE_PROPOSALS)
    for (kind, key), lanes in sorted(keys.items()):
        lanes = lanes | {row["lane"] for row in live if row["kind"] == kind and row["key"] == key}
        if len(lanes) > 1:
            print(f"conflict {kind} {key} lanes={','.join(sorted(lanes))}", file=stdout)
    print(f"file_conflicts={len(conflicts)} (not imported)", file=stdout)
    for item in conflicts:
        item = item if isinstance(item, dict) else {}
        print("file_conflict", item.get("kind") or "-", item.get("id") or "-",
              "suggested=" + str(item.get("suggested") or "-"), file=stdout)
    if unused:
        print(f"unused_in_config={len(unused)} (ignored)", file=stdout)
    if not write:
        print("dry run: nothing changed; rerun without --dry-run", file=stdout)
    return 0


def sent(args, store, config, stdout, stderr):
    """Record a lane's own post: sends row, thread binding, and a dm claim for DMs."""
    channel, ts, root = args.channel, args.ts, args.root_ts
    if not CONVERSATION.fullmatch(channel) or not SLACK_TS.fullmatch(ts):
        raise BadInput("Invalid channel or ts")
    if root is not None and not SLACK_TS.fullmatch(root):
        raise BadInput("Invalid thread ts")
    if root == ts:
        root = None
    kind = args.kind or ("reply" if root else "dm" if channel.startswith("D") else "root")
    if (kind == "reply") != (root is not None) and not (kind == "dm" and root is not None):
        raise BadInput("A reply needs --thread ROOT_TS; a root has none")
    if store.get_lane(args.lane) is None:
        print("Lane not found", file=stderr)
        return 3
    lane = args.lane
    created, previous = store.record_send(channel, ts, root, lane, kind)
    thread = f"slack:{channel}:{root or ts}"
    if not created:
        print(f"sent {channel} {ts} already recorded", file=stdout)
        return 0
    moved = f" (was {previous})" if previous not in (None, lane) else ""
    print(f"sent {channel} {ts} {lane} bound {thread}{moved}", file=stdout)
    if channel.startswith("D") or kind == "dm":
        claimed = store.add_scope_claim(lane, "dm", channel, claimed_by=claimant())
        store.record_scope_evidence("dm", channel, lane, "sent", min_evidence=min_evidence(config))
        print(f"dm {channel} {lane} {'claimed' if claimed else 'refreshed'}", file=stdout)
    return 0


OWNER_COLUMNS = ("id", "channel", "thread", "best_lane", "confidence", "source")
REGISTRY_COLUMNS = ("id", "state", "lane", "channel", "thread", "registry_id", "status",
                    "last_ts", "message_ts", "compare")


def owner_resolver(config):
    """The P17 resolver for on-demand CLI use (works with owner_resolver.enabled false)."""
    from slack_dispatch.resolver.owner import OwnerResolver
    return OwnerResolver(config)


def owner_command(args, store, config, stdout, stderr):
    """CLI `owner CHANNEL [--ts TS] [--json]` and `owner --pending [--apply] [--json]`."""
    from slack_dispatch.resolver.owner import VERSION
    if config is None:
        print("Config is required (--config or SLACK_DISPATCH_CONFIG)", file=stderr)
        return 2
    if bool(args.pending) == bool(args.channel) or (args.apply and not args.pending):
        raise BadInput("Supply a channel or --pending")
    resolver = owner_resolver(config)
    names = display_names(config, store)  # P20: channel and author names
    if not args.pending:
        if not CONVERSATION.fullmatch(args.channel) or (
                args.owner_ts is not None and not SLACK_TS.fullmatch(args.owner_ts)):
            raise BadInput("Invalid channel or ts")
        thread_ts = None
        if args.owner_ts:
            message = store.get_message(f"slack:{args.channel}:{args.owner_ts}") or {}
            if message.get("thread_ts") and message["thread_ts"] != args.owner_ts:
                thread_ts = message["thread_ts"]
        result = resolver.resolve(store, args.channel, args.owner_ts, thread_ts).as_dict()
        channel_name = names.channel_name(args.channel)
        if args.json:
            # The GET /api/owner shape, unchanged; the text line carries the channel name.
            print(json.dumps(result, ensure_ascii=False), file=stdout)
            return 0
        best = result["best"]
        print(f"channel {args.channel} root {result['root'] or '-'} best "
              + (f"{best['lane']} ({best['confidence']}, {best['source']})" if best else "none")
              + (f" name {text_label(channel_name)}" if channel_name else ""),
              file=stdout)
        row = result["registry_row"]
        if row:
            print("registry", row["id"] or "-", "status", row["status"] or "-",
                  "closed", "yes" if row["closed"] else "no", "last_ts", row["last_ts"] or "-",
                  file=stdout)
        print("confidence lane session source evidence", file=stdout)
        for candidate in result["candidates"]:
            print(candidate["confidence"], candidate["lane"] or "-", candidate["session"] or "-",
                  candidate["source"], candidate["evidence"], file=stdout)
        return 0
    rows = []
    for message in store.list_messages(state="human_pending", limit=2**63 - 1):
        resolution = resolver.resolve(store, message["channel"], message.get("ts"),
                                      message.get("thread_ts"))
        best = resolution.best
        rows.append({"id": message["id"], "channel": message["channel"],
                     "thread": message.get("thread_ts") or "-",
                     "best_lane": best.lane if best else None,
                     "confidence": best.tier if best else None,
                     "source": best.source if best else None,
                     "reason": resolution.reason(),
                     "channel_name": names.channel_name(message["channel"]),
                     "author": message.get("author"),
                     "author_name": names.user_name(message.get("author"))})
    applied = 0
    if args.apply:
        for row in rows:
            if row["best_lane"]:
                store.set_suggestion(row["id"], row["best_lane"], row["reason"], VERSION)
                applied += 1
    if args.json:
        print(json.dumps({"mode": "apply" if args.apply else "dry-run", "rows": rows,
                          "applied": applied}, ensure_ascii=False), file=stdout)
        return 0
    print(" ".join(OWNER_COLUMNS), file=stdout)
    for row in rows:
        # Ids stay in the table (scripts copy them); JSON rows carry the P20 names.
        print(*(row[key] or "-" for key in OWNER_COLUMNS), file=stdout)
    found = sum(1 for row in rows if row["best_lane"])
    print(f"pending={len(rows)} with_lane={found} applied={applied}", file=stdout)
    if not args.apply:
        print("dry run: nothing changed; rerun with --apply to write the suggestions", file=stdout)
    return 0


def registry_check(args, store, config, stdout, stderr):
    """CLI `registry check [--lane LANE] [--json]`: open items whose registry row is closed."""
    if config is None:
        print("Config is required (--config or SLACK_DISPATCH_CONFIG)", file=stderr)
        return 2
    if not config.owner_resolver.registry_path:
        print("owner_resolver.registry_path is not configured", file=stderr)
        return 2
    resolver = owner_resolver(config)
    rows = []
    for message in store.open_messages(lane=args.lane):
        info = resolver.registry_for(message)
        if not info or not info["closed"]:
            continue
        compare = {True: "newer", False: "older", None: "unknown"}[info["last_ts_newer"]]
        rows.append({"id": message["id"], "state": message["state"], "lane": message.get("lane"),
                     "channel": message["channel"], "thread": message.get("thread_ts"),
                     "registry_id": info["id"], "status": info["status"],
                     "last_ts": info["last_ts"], "message_ts": message["ts"], "compare": compare,
                     "registry_closed": info["registry_closed"], "note": info["note"]})
    candidates = sum(1 for row in rows if row["registry_closed"])
    if args.json:
        print(json.dumps({"rows": rows, "count": len(rows), "registry_closed": candidates},
                         ensure_ascii=False), file=stdout)
        return 0
    print(" ".join(REGISTRY_COLUMNS), file=stdout)
    for row in rows:
        print(*(row[key] or "-" for key in REGISTRY_COLUMNS), file=stdout)
    print(f"closed_in_registry={len(rows)} registry_closed={candidates} (last_ts at or after "
          "the message); nothing changed", file=stdout)
    return 0


def delivery_receipt_text(row):
    """Receipt column of `deliveries`: received, unconfirmed, a card decision, or -."""
    receipt = row["receipt"]
    if row["purpose"] == "lane":
        if receipt["received_time"]:
            return f"received {receipt['received_time']} (+{round((receipt['latency_seconds'] or 0) / 60)}m)"
        if row["status"] == "sent":
            return f"unconfirmed {round((receipt['age_seconds'] or 0) / 60)}m"
        return "-"
    return f"decision {receipt['decision_time']}" if receipt["decision_time"] else "-"


def delivery_line(row):
    """One `deliveries` text line: time, kind, lane, target, count, status, receipt."""
    target = row["target"]
    if target:
        name = target["name"] or "-"
        target_text = name + (f"({target['short_id']})" if target["short_id"] else "")
    elif row["adapter"] and row["adapter"] != "session_message":
        target_text = row["adapter"]
    else:
        target_text = "no-session"
    status = row["status"] or "-"
    if row["relay_outcome"]:
        status += "/" + row["relay_outcome"]
    if (row.get("repeat_count") or 1) > 1:
        status += f" x{row['repeat_count']}"
    return " ".join(str(part) for part in (row["when"] or "-", row["kind"] or "-", row["lane"] or "-",
                                          target_text.replace(" ", "_"), row["item_count"], status,
                                          delivery_receipt_text(row)))


def deliveries(args, store, config, stdout, stderr):
    """P18: delivery_log rows newest first, with receipts, as text lines or JSON."""
    from types import SimpleNamespace
    from slack_dispatch.console import queries
    if args.hours < 0:
        raise BadInput("Invalid hours")
    if args.status and args.status not in queries.DELIVERY_STATUS_FILTERS:
        raise BadInput("Invalid status")
    if config is None:
        config = SimpleNamespace(display_timezone="UTC", people={},
                                 console=SimpleNamespace(channels={}))
    view = queries.View(config).bind(store)  # P20: people, then the ledger, then ids
    now = DELIVERY_CLOCK()
    rows = store.list_deliveries(since=queries.delivery_since(now, args.hours), lane=args.lane,
                                 status=args.status or None) if hasattr(store, "list_deliveries") else []
    cache = {}
    shaped = [queries.delivery_row(store, view, row, cache)
              for row in store.delivery_receipts(rows, now)] if rows else []
    if args.json:
        print(json.dumps(shaped, ensure_ascii=False, indent=2), file=stdout)
        return 0
    if not shaped:
        print("No delivery attempts", file=stdout)
        return 0
    for row in shaped:
        print(delivery_line(row), file=stdout)
    return 0


def directory_refresh(args, store, config, stdout, stderr, *, client=None):
    """P20 `directory refresh [--missing-only] [--json]`: operator backfill of names.

    Looks up every distinct ledger author (`--missing-only`: only authors without a
    `user_directory` row) with read-only `users.info` calls through the reconcile source's
    client (`client` is injected by tests). Prints the counts; exit 1 when a lookup failed.
    """
    from slack_dispatch.ingest.user_directory import (DEFAULT_TTL_SECONDS, UserDirectory,
                                                      known_bot_users, open_reconcile_client)
    if client is None:
        if config is None:
            print("Config is required (--config or SLACK_DISPATCH_CONFIG)", file=stderr)
            return 2
        try:
            client = open_reconcile_client(config)
        except LookupError:
            print("No slack_reconcile source configured", file=stderr)
            return 2
        except Exception as error:
            # Class name only: the message could name the credential file.
            print("Slack client could not be opened class=" + type(error).__name__, file=stderr)
            return 2
    settings = getattr(config, "user_directory", None)
    directory = UserDirectory(client, store, getattr(settings, "ttl_seconds", DEFAULT_TTL_SECONDS),
                              known_bot_users(config))
    users = store.authors_without_directory() if args.missing_only else store.distinct_authors()
    named = failed = 0
    for user in users:
        row = directory.refresh(user)
        if row is None:
            failed += 1
        elif row.get("name"):
            named += 1
    result = {"mode": "missing-only" if args.missing_only else "all", "authors": len(users),
              "looked_up": directory.lookups, "named": named, "failed": failed}
    if args.json:
        print(json.dumps(result), file=stdout)
    else:
        print("directory refresh " + " ".join(f"{key}={value}" for key, value in result.items()),
              file=stdout)
    return 1 if failed else 0


def configured_session(config_path, lane):
    """Return the lane's configured session from an optional config file, else None."""
    if not config_path:
        return None
    from slack_dispatch.config.loader import load_config
    configured = next((row for row in load_config(config_path).lanes if row.id == lane), None)
    return configured.session if configured else None


def main(argv=None, *, store=None, stdout=None, stderr=None, client=None):
    # `client` (P20) is the Slack client for `directory refresh`; tests inject a fake.
    # Console streams use UTF-8 so non-ASCII text survives a legacy code page (cp949).
    # Injected streams are the caller's and are left unchanged.
    if stdout is None:
        stdout = sys.stdout
        use_utf8(stdout)
    if stderr is None:
        stderr = sys.stderr
        use_utf8(stderr)
    owned = False
    error_reported = False
    def run():
        nonlocal store, owned, error_reported
        try:
            args = build_parser().parse_args(argv)
            root = args.inbox_root or os.environ.get("SLACK_DISPATCH_INBOX", "inbox")
            if getattr(args, "lane", None) is not None:
                validate_lane(args.lane)
            if args.command in ("ack", "done"):
                if bool(args.ids) == bool(args.thread) or (args.lane and not args.thread):
                    raise BadInput("Supply ids or a thread root")
            if args.command == "claim":
                session = args.session or os.environ.get("SLACK_DISPATCH_SESSION") or os.environ.get("CLAUDE_SESSION_ID")
                if not session:
                    raise BadInput("Session is required")
                configured = configured_session(args.config or os.environ.get("SLACK_DISPATCH_CONFIG"),
                                                args.lane)
            if args.command == "inbox":
                # Optional display settings: people names and display_timezone; since P20
                # also channel names and console.workspace_domain from the same config.
                people, zone, display = display_config(
                    args.config or os.environ.get("SLACK_DISPATCH_CONFIG"), stderr)
            # P13: scope and sent read the optional config strictly (scope:auto, min_evidence);
            # assign and reroute only borrow min_evidence and never fail on the config.
            config = None
            if args.command in ("scope", "sent", "owner", "registry", "directory"):
                config = optional_config(args)
            elif args.command in ("assign", "reroute", "queue"):
                config = optional_config(args, strict=False)
            if store is None:
                path = args.ledger or os.environ.get("SLACK_DISPATCH_LEDGER")
                if not path:
                    raise BadInput("Ledger path is required")
                from slack_dispatch.ledger.store import LedgerStore
                store = LedgerStore(path)
                store.open()
                owned = True
                store.migrate()
            if hasattr(args, "id"):
                message = store.get_message(args.id)
                if message is None:
                    print("Message not found", file=stderr)
                    error_reported = True
                    return 3
            if args.command == "import-bindings":
                rows = []
                for line in Path(args.path).read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if not isinstance(row, dict) or any(
                            not isinstance(row.get(key), str) or not row[key].strip()
                            for key in ("source", "channel", "thread_ts", "lane")):
                        raise BadInput("Invalid binding row")
                    validate_lane(row["lane"])
                    rows.append(row)
                added = kept = skipped = 0
                unknown = set()
                for row in rows:
                    lane = row["lane"]
                    if store.get_lane(lane) is None:
                        skipped += 1
                        unknown.add(lane)
                        continue
                    key = (row["source"], row["channel"], row["thread_ts"])
                    if store.lookup_binding(*key) is not None and not args.replace:
                        kept += 1
                        continue
                    store.bind_thread(*key, lane)
                    added += 1
                print(f"added={added} kept={kept} skipped unknown lane={skipped}", file=stdout)
                if unknown:
                    print("Unknown lanes: " + ", ".join(sorted(unknown)), file=stdout)
            elif args.command == "inbox":
                from functools import partial
                from slack_dispatch.console.queries import workspace_domain
                # P20: JSON rows carry `permalink` whenever the domain is configured.
                links = partial(permalink_for, display) if workspace_domain(display) else None
                show_inbox(args, root, store, stdout, people, zone,
                           names=display_names(display, store), links=links)
            elif args.command == "directory":
                code = directory_refresh(args, store, config, stdout, stderr, client=client)
                error_reported = code != 0
                return code
            elif args.command in ("scope", "sent"):
                if args.command == "sent":
                    code = sent(args, store, config, stdout, stderr)
                elif args.scope_command == "add":
                    code = scope_add(args, store, config, stdout, stderr)
                elif args.scope_command == "list":
                    code = scope_list(args, store, stdout)
                elif args.scope_command == "remove":
                    code = scope_remove(args, store, stdout, stderr)
                else:
                    code = scope_import(args, store, config, stdout)
                error_reported = code != 0
                return code
            elif args.command in ("owner", "registry"):
                if args.command == "owner":
                    code = owner_command(args, store, config, stdout, stderr)
                else:
                    code = registry_check(args, store, config, stdout, stderr)
                error_reported = code != 0
                return code
            elif args.command == "cutover":
                code = cutover(args, root, store, stdout, stderr)
                error_reported = code != 0
                return code
            elif args.command == "repair":
                code = repair_command(args, root, store, stdout, stderr)
                error_reported = code != 0
                return code
            elif args.command == "deliveries":
                return deliveries(args, store, optional_config(args), stdout, stderr)
            elif args.command == "undo":
                code = actions.undo_command(store, last=args.last, action_id=args.action_id,
                                            as_json=args.json, stdout=stdout, stderr=stderr,
                                            now=UNDO_CLOCK())
                error_reported = code != 0
                return code
            elif args.command == "queue":
                # P20 fix1: the existing `id suggestion reason`, then the channel and author
                # display names (people, directory, id; spaces become `_`).
                names = display_names(config, store)
                for row in store.list_messages(state="human_pending"):
                    message_id = row.get("id", row.get("message_id"))
                    print(message_id,
                          row.get("classifier_suggestion", row.get("suggested_lane")) or "none", row.get("suggestion_reason") or "none",
                          text_label(names.channel(row.get("channel"))),
                          text_label(names.user(row.get("author"))), file=stdout)
                    if args.show_body:
                        full = store.get_message(message_id, include_body=True)
                        if full and full.get("text", full.get("body")) is not None:
                            print(message_id, full.get("text", full.get("body")), file=stdout)
            elif args.command == "claim":
                # The value is stored verbatim: a session name, a session id or an id prefix.
                store.upsert_lane(args.lane, session_pointer=session)
                print(args.lane, "claimed", file=stdout)
                if configured:
                    print(f"warning: lane {args.lane} has a configured session; config wins "
                          "over this claim until the configured session is null", file=stderr)
            elif args.command in ("ack", "done"):
                ids = bulk_ids(args, root, store, stderr)
                if ids is None:
                    print("Message not found: " + args.thread, file=stderr)
                    error_reported = True
                    return 3
                failed = False
                for mid in ids:
                    try:
                        if store.get_message(mid) is None:
                            print("Message not found: " + mid, file=stderr)
                            failed = True
                            continue
                        store.set_state(mid, "acked" if args.command == "ack" else "done", note=getattr(args, "note", None))
                    except Exception:
                        if len(ids) == 1 and not args.thread:
                            raise
                        print("Inbox operation failed for id: " + mid, file=stderr)
                        failed = True
                        continue
                    print(mid, args.command, file=stdout)
                if failed:
                    error_reported = True
                    return 3
            else:
                if store.get_lane(args.lane) is None:
                    print("Lane not found", file=stderr)
                    error_reported = True
                    return 3
                refusal = inbox_refusal(dict(message, id=args.id))
                if refusal:
                    print(refusal, file=stderr)
                    error_reported = True
                    return 2
                # One code path with the console: route, inbox append and a wake request.
                if args.command == "assign":
                    actions.assign(store, root, args.id, args.lane, note=getattr(args, "note", None),
                                   reason="human:cli", min_evidence=min_evidence(config))
                else:
                    actions.reroute(store, root, args.id, args.lane, source="cli",
                                    min_evidence=min_evidence(config))
                print(args.id, args.lane, file=stdout)
            return 0
        except AmbiguousLane as exc:
            print(str(exc), file=stderr)
            error_reported = True
            return 2
        except (BadInput, ValueError):
            print("Invalid input", file=stderr)
            error_reported = True
            return 2
        except Exception:
            print("Inbox operation failed", file=stderr)
            error_reported = True
            return 1

    close_failed = False
    try:
        try:
            result = run()
        finally:
            if owned:
                try:
                    store.close()
                except Exception:
                    if not error_reported:
                        print("Inbox operation failed", file=stderr)
                    close_failed = True
    except BaseException:
        if not close_failed:
            raise
    return 1 if close_failed else result


if __name__ == "__main__":
    raise SystemExit(main())
