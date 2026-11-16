import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from slack_dispatch.inbox.cli import main
from slack_dispatch.inbox.files import record
from slack_dispatch.ledger.store import LedgerStore


class InboxViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = LedgerStore(self.root / "dummy.db", store_body=True)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        self.store.upsert_lane("support")
        self.store.upsert_lane("other")

    def add(self, ts, thread=None, body="DUMMY_BODY", state="assigned",
            author="UAUTHOR", reason="test", lane="support", deliver=True):
        mid = "slack:CDUMMY:" + ts
        self.store.record_event(dict(source="slack", event_type="message",
                                     event_id="dummy-event-" + ts, channel="CDUMMY",
                                     ts=ts, thread_ts=thread, user=author, text=body,
                                     received_at="2026-01-01T00:00:00+00:00"))
        if lane is None:
            self.store.set_state(mid, state)
        else:
            self.store.set_route(mid, lane, reason, state=state)
        if deliver:
            self.deliver(mid)
        return mid

    def deliver(self, mid, kind="new", stamp=None, lane="support", followup=False):
        row = record(self.store.get_message(mid), "new")
        row.update(kind=kind, followup=followup)
        if stamp:
            row["delivered_at"] = stamp
        with (self.root / (lane + ".jsonl")).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = main(["--inbox-root", str(self.root), *args], store=self.store,
                    stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def payload(self, *args):
        code, out, err = self.cli(*args, "--json")
        self.assertEqual((code, err), (0, ""))
        return json.loads(out)

    def test_collapse_metadata_age_and_unknown_kinds(self):
        first = self.add("10", deliver=False)
        second = self.add("11", deliver=False)
        self.deliver(second, stamp="2026-01-02T00:00:00+00:00")
        self.deliver(first, "backfill", "2026-01-01T00:00:00+00:00")
        self.deliver(first, "followup", "2026-01-03T00:00:00+00:00")
        self.deliver(first, "future-kind", "2026-01-04T00:00:00+00:00")
        self.store.set_attention(first, "owner_mention")
        with patch("slack_dispatch.inbox.cli.datetime") as clock:
            clock.fromisoformat.side_effect = datetime.fromisoformat
            clock.now.return_value = datetime(2026, 1, 5, tzinfo=timezone.utc)
            rows = self.payload("inbox", "support")
        self.assertEqual([row["id"] for row in rows], [first, second])
        # P9: rows add `when`, and JSON carries the raw ts and author_id.
        self.assertEqual(rows[0], dict(id=first, age="345600s", when="01-01 00:00", channel="CDUMMY",
                                      thread="-", kind="future-kind", edited="yes",
                                      attention="owner_mention", author="UAUTHOR", state="assigned",
                                      ts="10", author_id="UAUTHOR"))
        self.assertEqual(rows[1]["edited"], "no")
        text = self.cli("inbox", "support")[1]
        self.assertEqual(text.splitlines()[0],
                         "id age when channel thread kind edited attention author state"
                         " channel_name author_name")
        self.assertEqual(sum(line.startswith(first) for line in text.splitlines()), 1)
        self.assertNotIn("DUMMY_BODY", text)
        self.assertEqual(self.payload("inbox", "support", "--show-body")[0]["body"], "DUMMY_BODY")

    def test_ledger_edit_and_followup_only_default(self):
        mid = self.add("1", deliver=False)
        self.deliver(mid, "followup")
        row = self.payload("inbox", "support")[0]
        self.assertEqual((row["kind"], row["edited"]), ("new", "yes"))
        other = self.add("2")
        self.store.record_event(dict(source="slack", event_type="message", event_id="dummy-edit",
                                     subtype="message_changed", channel="CDUMMY", ts="2",
                                     user="UAUTHOR", received_at="2026-01-02T00:00:00+00:00"))
        rows = {row["id"]: row for row in self.payload("inbox", "support")}
        self.assertEqual(rows[other]["edited"], "yes")

    def test_thread_context_owner_order_full_and_body_controls(self):
        root = self.add("1", body="R" * 400 + "TAIL", state="done", reason="context:root", deliver=False)
        later = self.add("10", thread="1", body="LATER_BODY")
        early = self.add("2", thread="1", body="EARLY_BODY")
        self.add("11", thread="1", author="UOWNER", reason="owner_post", state="done", deliver=False)
        group = self.payload("inbox", "support", "--thread")[0]
        self.assertEqual(group["root"]["id"], root)
        self.assertEqual(group["root"]["body"], "R" * 400)
        self.assertEqual([item["id"] for item in group["items"]], [early, later])
        self.assertEqual(group["owner_replied_at"], "11")
        text = self.cli("inbox", "support", "--thread")[1]
        self.assertIn("owner replied at 11", text)
        self.assertIn("EARLY_BODY", text)
        self.assertNotIn("TAIL", text)
        self.assertIn("TAIL", self.cli("inbox", "support", "--thread", "--full")[1])
        with patch.object(self.store, "get_message", wraps=self.store.get_message) as lookup:
            groups = self.payload("inbox", "support", "--thread", "--no-body")
            self.assertFalse(any(call.kwargs.get("include_body") for call in lookup.call_args_list))
        self.assertNotIn("body", groups[0]["root"])
        self.assertTrue(all("body" not in item for item in groups[0]["items"]))
        text = self.cli("inbox", "support", "--thread", "--no-body", "--show-body")[1]
        self.assertNotIn("EARLY_BODY", text)

    def test_missing_root_null_body_and_root_once(self):
        root = self.add("1", body=None)
        missing_reply = self.add("21", thread="20", body=None)
        groups = self.payload("inbox", "support", "--thread")
        self.assertEqual(groups[0]["root"]["id"], root)
        self.assertEqual(groups[0]["items"], [])
        self.assertEqual(groups[0]["root"]["kind"], "new")
        self.assertTrue(groups[1]["root"]["missing"])
        self.assertEqual(groups[1]["items"][0]["id"], missing_reply)
        self.assertEqual(groups[1]["items"][0]["body"], "[no body stored]")
        text = self.cli("inbox", "support", "--thread")[1]
        self.assertIn("root not in ledger", text)
        self.assertEqual(sum(line.startswith(root + " ") for line in text.splitlines()), 1)

    def test_owner_option_and_lookup_beyond_default_limit(self):
        self.add("1", state="done", deliver=False)
        self.add("2", thread="1")
        self.add("1.5", thread="1", author="UOWNER", deliver=False)
        self.assertNotIn("owner replied", self.cli("inbox", "support", "--thread", "--owner", "UOWNER")[1])
        for index in range(100, 205):
            self.add(str(index), deliver=False)
        self.add("999", thread="1", author="UOWNER", deliver=False)
        self.assertNotIn("owner replied", self.cli("inbox", "support", "--thread")[1])
        self.assertIn("owner replied at 999", self.cli("inbox", "support", "--thread", "--owner", "UOWNER")[1])

    def test_include_done_and_obsolete_lane_filter(self):
        root = self.add("1", state="done")
        moved = self.add("2")
        self.store.set_route(moved, "other", "test")
        for options in ([], ["--thread"]):
            self.assertEqual(self.payload("inbox", "support", *options), [])
            rows = self.payload("inbox", "support", *options, "--include-done")
            self.assertEqual(len(rows), 1)
            self.assertIn(root, str(rows))
            self.assertNotIn(moved, str(rows))

    def test_bulk_ack_done_partial_failure_and_notes(self):
        first, second = self.add("1"), self.add("2")
        code, out, err = self.cli("ack", first, "slack:CDUMMY:999", second, first)
        self.assertEqual(code, 3)
        self.assertIn("slack:CDUMMY:999", err)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertEqual(self.store.get_message(second)["state"], "acked")
        self.assertEqual(self.cli("done", first, second, "--note", "dummy-note")[0], 0)
        self.assertEqual(self.store.get_message(first)["note"], "dummy-note")
        self.assertEqual(self.payload("inbox", "support"), [])
        for args in (("ack",), ("done",), ("done", first, "--thread", first),
                     ("ack", "--thread", "bad"), ("ack", "--thread", "slack:CDUMMY:no-ts"),
                     ("ack", first, "--lane", "support")):
            self.assertEqual(self.cli(*args)[0], 2, args)
        self.assertEqual(self.cli("done", "--thread", "slack:CDUMMY:999")[0], 3)

    def test_thread_bulk_scope_and_missing_root_inference(self):
        root = self.add("1", state="done", reason="context:root", deliver=False)
        first, second = self.add("2", thread="1"), self.add("3", thread="1")
        other = self.add("4", thread="1", lane="other", deliver=False)
        self.deliver(other, lane="other")
        outside = self.add("5", thread="1", deliver=False)
        self.assertEqual(self.cli("ack", "--thread", root)[0], 0)
        self.assertEqual(self.store.get_message(first)["state"], "acked")
        self.assertEqual(self.cli("done", "--thread", root, "--note", "thread-note")[0], 0)
        self.assertEqual(self.store.get_message(second)["note"], "thread-note")
        self.assertEqual(self.store.get_message(other)["state"], "assigned")
        self.assertEqual(self.store.get_message(outside)["state"], "assigned")
        self.assertEqual(self.cli("done", "--thread", root)[0], 0)
        missing = self.add("21", thread="20")
        self.assertEqual(self.cli("ack", "--thread", "slack:CDUMMY:20")[0], 0)
        self.assertEqual(self.cli("done", "--thread", "slack:CDUMMY:20", "--lane", "support")[0], 0)
        self.assertEqual(self.store.get_message(missing)["state"], "done")
        self.assertEqual(self.cli("ack", "--thread", first)[0], 2)

    def test_ambiguous_thread_requires_lane(self):
        first = self.add("2", thread="1")
        other = self.add("3", thread="1", lane="other", deliver=False)
        self.deliver(other, lane="other")
        self.assertEqual(self.cli("ack", "--thread", "slack:CDUMMY:1")[0], 2)
        self.assertEqual(self.store.get_message(first)["state"], "assigned")
        self.assertEqual(self.cli("ack", "--thread", "slack:CDUMMY:1", "--lane", "other")[0], 0)
        self.assertEqual(self.store.get_message(other)["state"], "acked")

    def test_thread_bulk_falls_back_to_reroute_lane(self):
        root = self.add("1", state="done")
        reply = self.add("2", thread="1")
        self.assertEqual(self.cli("reroute", reply, "--to", "other")[0], 0)
        code, out, err = self.cli("ack", "--thread", root, "--lane", "support")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(err, f"no pending items for thread {root} in lane support\n")
        self.assertEqual(self.store.get_message(reply)["state"], "assigned")
        code, out, err = self.cli("done", "--thread", root)
        self.assertEqual((code, out, err), (0, reply + " done\n", ""))
        self.assertEqual(self.store.get_message(reply)["state"], "done")
        code, out, err = self.cli("done", "--thread", root)
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(err, f"no pending items for thread {root} in lane support\n")

    def test_thread_bulk_context_root_without_lane(self):
        root = self.add("1", state="done", reason="context:root", lane=None, deliver=False)
        reply = self.add("2", thread="1")
        self.add("3", thread="1", author="UOWNER", reason="owner_post", state="done",
                 lane="other", deliver=False)
        self.assertIsNone(self.store.get_message(root)["lane"])
        code, out, err = self.cli("done", "--thread", root)
        self.assertEqual((code, out, err), (0, reply + " done\n", ""))
        self.assertEqual(self.store.get_message(reply)["state"], "done")
        code, out, err = self.cli("ack", "--thread", root)
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(err, f"no pending items for thread {root} in any lane\n")

    def test_thread_bulk_pending_in_two_lanes_is_ambiguous(self):
        root = self.add("1", state="done", reason="context:root", lane=None, deliver=False)
        first = self.add("2", thread="1")
        second = self.add("3", thread="1", lane="other", deliver=False)
        self.deliver(second, lane="other")
        code, out, err = self.cli("done", "--thread", root)
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, f"ambiguous lane for thread {root}: other, support (use --lane)\n")
        self.assertEqual(self.store.get_message(first)["state"], "assigned")
        self.assertEqual(self.store.get_message(second)["state"], "assigned")

    def test_bulk_storage_failure_continues_and_is_sanitized(self):
        first, failed, last = self.add("1"), self.add("2"), self.add("3")
        original = self.store.set_state
        def update(mid, state, **kwargs):
            if mid == failed:
                raise RuntimeError("PRIVATE_ERROR")
            return original(mid, state, **kwargs)
        for command in ("ack", "done"):
            with patch.object(self.store, "set_state", side_effect=update):
                code, out, err = self.cli(command, first, failed, last)
            self.assertEqual(code, 3)
            self.assertIn(failed, err)
            self.assertNotIn("PRIVATE_ERROR", err)
            self.assertIn(first, out)
            self.assertIn(last, out)
        self.assertEqual(self.store.get_message(first)["state"], "done")
        self.assertEqual(self.store.get_message(failed)["state"], "assigned")
        self.assertEqual(self.store.get_message(last)["state"], "done")
