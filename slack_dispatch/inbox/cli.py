"""Plain-text inbox commands against the shared LedgerStore API."""
import argparse
import json
from pathlib import Path
import os
import sys
from datetime import datetime, timezone

from .files import append, read, record, validate_lane


class BadInput(ValueError):
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


def main(argv=None, *, store=None, stdout=None, stderr=None):
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    owned = False
    error_reported = False
    def run():
        nonlocal store, owned, error_reported
        try:
            args = build_parser().parse_args(argv)
            root = args.inbox_root or os.environ.get("SLACK_DISPATCH_INBOX", "inbox")
            if hasattr(args, "lane"):
                validate_lane(args.lane)
            if args.command == "claim":
                session = args.session or os.environ.get("SLACK_DISPATCH_SESSION") or os.environ.get("CLAUDE_SESSION_ID")
                if not session:
                    raise BadInput("Session is required")
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
                print("id age channel thread followup state", file=stdout)
                for row in read(root, args.lane, include_done=args.include_done, store=store):
                    print(row["id"], age(row), row["channel"], bool(row.get("thread_ts") and row["thread_ts"] != row["ts"]),
                          bool(row["followup"]), row.get("state", "unknown"), file=stdout)
                    if args.show_body:
                        full = store.get_message(row["id"], include_body=True)
                        if full and full.get("text", full.get("body")) is not None:
                            print(row["id"], full.get("text", full.get("body")), file=stdout)
            elif args.command == "queue":
                for row in store.list_messages(state="human_pending"):
                    message_id = row.get("id", row.get("message_id"))
                    print(message_id, row.get("classifier_suggestion", row.get("suggested_lane")) or "none", row.get("suggestion_reason") or "none", file=stdout)
                    if args.show_body:
                        full = store.get_message(message_id, include_body=True)
                        if full and full.get("text", full.get("body")) is not None:
                            print(message_id, full.get("text", full.get("body")), file=stdout)
            elif args.command == "claim":
                store.upsert_lane(args.lane, session_pointer=session)
                print(args.lane, "claimed", file=stdout)
            elif args.command in ("ack", "done"):
                store.set_state(args.id, "acked" if args.command == "ack" else "done", note=getattr(args, "note", None))
                print(args.id, args.command, file=stdout)
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
