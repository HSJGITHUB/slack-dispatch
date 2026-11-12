"""Plain-text inbox commands against the shared LedgerStore API."""
import argparse
import json
from pathlib import Path
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from .files import append, read, record, validate_lane

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


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
            sub.add_argument("--config")
        if command == "inbox":
            sub.add_argument("--config")
            sub.add_argument("--include-done", action="store_true")
            sub.add_argument("--thread", action="store_true")
            sub.add_argument("--json", action="store_true")
            sub.add_argument("--full", action="store_true")
            sub.add_argument("--no-body", action="store_true")
            sub.add_argument("--owner")
    sub = commands.add_parser("import-bindings")
    options(sub)
    sub.add_argument("path")
    sub.add_argument("--replace", action="store_true")
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
    if not config_path:
        return {}, timezone.utc
    from slack_dispatch.config.loader import load_config
    config = load_config(config_path)
    return dict(config.people), display_zone(config.display_timezone, stderr)


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
        row["state"] = message.get("state") or "unknown"
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
# JSON rows also carry the raw message ts and the raw author id.
JSON_EXTRA = ("ts", "author_id")


def show_inbox(args, root, store, stdout, people=None, zone=timezone.utc):
    people = people or {}
    rows = inbox_rows(root, args.lane, store, args.include_done)
    for row in rows:
        row["when"] = when(row.get("ts"), zone)
        row["author_id"] = row["author"]
        row["author"] = people.get(row["author"], row["author"])
    if not args.thread:
        output = [{key: row[key] for key in LIST_COLUMNS + (JSON_EXTRA if args.json else ())}
                  for row in rows]
        if args.show_body and not args.no_body:
            for row in output:
                row["body"] = stored_body(store, row["id"])
        if args.json:
            print(json.dumps(output, ensure_ascii=False), file=stdout)
        else:
            print(" ".join(LIST_COLUMNS), file=stdout)
            for row in output:
                print(*(row[key] for key in LIST_COLUMNS), file=stdout)
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
                    "when": when(root_ts, zone), "author": people.get(author, author),
                    "author_id": author,
                    "state": (message or {}).get("state") or "unknown",
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
            if not args.no_body:
                item["body"] = stored_body(store, row["id"])
            items.append(item)
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
    for group in output:
        row = group["root"]
        print(row["id"], "channel", row["channel"], "author", row["author"],
              "ts", row["ts"], "when", row["when"], "state", row["state"],
              "root not in ledger" if row["missing"] else "root", file=stdout)
        if "kind" in row:
            print("kind", row["kind"], "edited", row["edited"], "attention", row["attention"], file=stdout)
        if "body" in row:
            print(row["body"], file=stdout)
        for item in group["items"]:
            print(*(item[key] for key in ITEM_COLUMNS), file=stdout)
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


def configured_session(config_path, lane):
    """Return the lane's configured session from an optional config file, else None."""
    if not config_path:
        return None
    from slack_dispatch.config.loader import load_config
    configured = next((row for row in load_config(config_path).lanes if row.id == lane), None)
    return configured.session if configured else None


def main(argv=None, *, store=None, stdout=None, stderr=None):
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
                # Optional display settings: people names and display_timezone.
                people, zone = display_settings(args.config or os.environ.get("SLACK_DISPATCH_CONFIG"),
                                                stderr)
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
                show_inbox(args, root, store, stdout, people, zone)
            elif args.command == "queue":
                for row in store.list_messages(state="human_pending"):
                    message_id = row.get("id", row.get("message_id"))
                    print(message_id, row.get("classifier_suggestion", row.get("suggested_lane")) or "none", row.get("suggestion_reason") or "none", file=stdout)
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
                reason = "human:cli" if args.command == "assign" else "reroute:" + str(message.get("lane") or "none")
                store.set_route(args.id, args.lane, reason)
                if getattr(args, "note", None):
                    store.set_state(args.id, "assigned", note=args.note)
                append(root, args.lane, [record(dict(message, id=args.id), "reroute" if args.command == "reroute" else "new")])
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
