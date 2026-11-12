"""Offline coverage tests with fake Slack pages and an in-memory ledger."""
from dataclasses import replace
import unittest
from unittest.mock import Mock

from slack_dispatch.config.schema import SourceConfig
from slack_dispatch.ingest.normalize import normalize_slack_event
from slack_dispatch.ingest.slack_reconcile import SlackReconcileSource
from slack_dispatch.ledger.store import LedgerStore

STAMP = "2026-01-01T00:00:00+00:00"


class CoverageIngestTests(unittest.TestCase):
    def setUp(self):
        self.store = LedgerStore(":memory:", store_body=True)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        self.store.upsert_lane("dummy")
        self.store.set_cursor("slack", "CDUMMY", "400000")
        self.events = []
        def receive(event):
            self.events.append(event)
            return self.store.record_event(event)
        self.source = SlackReconcileSource(SourceConfig("slack_reconcile", True, 3600, store_body=True),
                                           receive, "dummy.env", "DUMMY", self.store, clock=lambda: 1000000)
        self.source.owner_user = "UOWNER"
        self.source.client = Mock()

    def test_pages_order_overlap_owner_marking_and_shared_history(self):
        calls = []
        def respond(method, **kwargs):
            params = kwargs["params"]
            calls.append((method, params))
            if method == "conversations.history":
                return {"messages": [dict(ts="500000", user="UOTHER", reply_count=3, latest_reply="999950")]}
            if not params.get("cursor"):
                return {"messages": [dict(ts="999940", user="UOTHER"), dict(ts="500000", user="UOTHER")],
                        "response_metadata": {"next_cursor": "next"}}
            return {"messages": [dict(ts="999950", user="UOWNER"), dict(ts="999930", user="UOTHER")]}
        self.source.client.api_call.side_effect = respond
        self.source.catch_up()
        self.assertEqual([row["ts"] for row in self.events], ["500000", "999930", "999940", "999950"])
        self.assertTrue(all(row["origin"] == "reconcile" for row in self.events))
        self.assertEqual([row.get("prehandled_by") for row in self.events], ["owner_reply:999950"] * 3 + [None])
        self.assertEqual(self.store.get_cursor("slack_replies", "CDUMMY"), "1000000.000000")
        self.assertEqual(sum(method == "conversations.history" for method, _ in calls), 1)
        self.source.catch_up()
        self.assertEqual(self.source.summary["duplicates"], 3)
        self.assertEqual(len(self.store.list_messages()), 4)
        self.assertEqual(calls[-1][1]["oldest"], "999880.000000")

    def test_callback_return_value_is_not_a_required_contract(self):
        self.source.on_event = Mock()
        self.source.client.api_call.return_value = {"messages": [dict(ts="500000", user="UOTHER")]}
        self.source.catch_up()
        self.source.on_event.assert_called_once()
        self.assertEqual(self.source.failures, 0)
        self.assertEqual(self.store.get_cursor("slack", "CDUMMY"), "500000")

    def test_bound_old_thread_and_recent_ledger_thread_union(self):
        self.store.bind_thread("slack", "CDUMMY", "100", "dummy")
        self.store.record_event(dict(source="slack", event_id="EvDUMMY", event_type="message", channel="CDUMMY",
                                     ts="999900", thread_ts="200", user="UOTHER", received_at=STAMP))
        self.source.client.api_call.return_value = {"messages": []}
        self.source.catch_up()
        replies = [call.kwargs["params"] for call in self.source.client.api_call.call_args_list
                   if call.args[0] == "conversations.replies"]
        self.assertEqual({row["ts"] for row in replies}, {"100", "200"})
        self.assertEqual(self.source.summary["parents"], 2)

    def test_failure_preserves_replies_cursor_and_logs_only_error_code(self):
        self.store.set_cursor("slack_replies", "CDUMMY", "999900")
        self.store.bind_thread("slack", "CDUMMY", "100", "dummy")
        self.source.client.api_call.side_effect = [{"messages": []}, {"ok": False, "error": "internal_error"}]
        with self.assertLogs("slack_dispatch.ingest.slack_reconcile", level="INFO") as logs:
            self.source.catch_up()
        self.assertEqual(self.store.get_cursor("slack_replies", "CDUMMY"), "999900")
        self.assertEqual(self.source.failures, 1)
        self.assertIn("error=internal_error", " ".join(logs.output))
        self.assertIn("failures=1", " ".join(logs.output))

    def test_failed_parent_is_isolated_from_roots_and_other_parents(self):
        self.store.set_cursor("slack_replies", "CDUMMY", "999000")
        self.store.bind_thread("slack", "CDUMMY", "200", "dummy")
        def respond(method, **kwargs):
            params = kwargs["params"]
            if method == "conversations.history":
                return {"messages": [dict(ts="999500", user="UOTHER", reply_count=1, latest_reply="999800")]}
            if params["ts"] == "999500":
                return {"ok": False, "error": "fatal_error"}
            return {"messages": [dict(ts="999700", user="UOTHER")]}
        self.source.client.api_call.side_effect = respond
        with self.assertLogs("slack_dispatch.ingest.slack_reconcile", level="INFO") as logs:
            self.source.catch_up()
        self.assertEqual([row["ts"] for row in self.events], ["999500", "999700"])
        self.assertNotIn("prehandled_by", self.events[0])
        self.assertEqual(self.store.get_cursor("slack", "CDUMMY"), "999500")
        self.assertEqual(self.store.get_cursor("slack_replies", "CDUMMY"), "999000")
        self.assertEqual((self.source.failures, self.source.summary["parents"]), (1, 2))
        warning = [line for line in logs.output if line.startswith("WARNING")]
        self.assertEqual(len(warning), 1)
        self.assertIn("channel=CDUMMY parent=999500 class=RuntimeError error=fatal_error", warning[0])
        self.assertIn("failures=1", logs.output[-1])

    def test_permanent_parent_error_is_skipped_and_replies_cursor_advances(self):
        self.store.set_cursor("slack_replies", "CDUMMY", "999000")
        for thread in ("200", "300"):
            self.store.bind_thread("slack", "CDUMMY", thread, "dummy")
        calls = []
        def respond(method, **kwargs):
            params = kwargs["params"]
            if method == "conversations.history":
                return {"messages": []}
            calls.append(params["ts"])
            if params["ts"] == "200":
                return {"ok": False, "error": "thread_not_found"}
            return {"messages": [dict(ts="999700", user="UOTHER")]}
        self.source.client.api_call.side_effect = respond
        with self.assertLogs("slack_dispatch.ingest.slack_reconcile", level="INFO") as logs:
            self.source.catch_up()
            self.source.catch_up()
        self.assertEqual(self.source.skipped_parents, {("CDUMMY", "200")})
        self.assertEqual((self.source.failures, self.source.summary["failures"]), (0, 0))
        self.assertEqual(self.source.summary["permanent_skipped"], 1)
        self.assertEqual(calls, ["200", "300", "300"])
        self.assertEqual(self.store.get_cursor("slack_replies", "CDUMMY"), "1000000.000000")
        self.assertIsNotNone(self.source.last_catchup_at)
        # The advanced replies cursor moves the second pass past the delivered reply.
        self.assertEqual([row["ts"] for row in self.events], ["999700"])
        warnings = [line for line in logs.output if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 1)
        self.assertIn("channel=CDUMMY parent=200 error=thread_not_found", warnings[0])
        self.assertIn("failures=0", logs.output[-1])
        self.assertIn("permanent_skipped=1", logs.output[-1])

    def test_transient_parent_error_keeps_failures_and_replies_cursor(self):
        self.store.set_cursor("slack_replies", "CDUMMY", "999000")
        self.store.bind_thread("slack", "CDUMMY", "200", "dummy")
        responses = iter([{"ok": False, "error": "ratelimited"}, RuntimeError("private fixture text")])
        calls = []
        def respond(method, **kwargs):
            if method == "conversations.history":
                return {"messages": []}
            calls.append(kwargs["params"]["ts"])
            result = next(responses)
            if isinstance(result, Exception):
                raise result
            return result
        self.source.client.api_call.side_effect = respond
        for expected in (1, 2):
            self.source.catch_up()
            self.assertEqual(self.source.failures, expected)
            self.assertEqual(self.source.summary["permanent_skipped"], 0)
        self.assertEqual(calls, ["200", "200"])
        self.assertEqual(self.source.skipped_parents, set())
        self.assertEqual(self.store.get_cursor("slack_replies", "CDUMMY"), "999000")
        self.assertIsNone(self.source.last_catchup_at)

    def test_bound_threads_are_capped_by_thread_activity(self):
        self.source.clock = lambda: 2000000
        for thread, ts in (("300", "1000000"), ("400", "500000")):
            self.store.record_event(dict(source="slack", event_id="Ev" + thread, event_type="message",
                                         channel="CDUMMY", ts=ts, thread_ts=thread, user="UOTHER",
                                         received_at=STAMP))
        for thread in ("100", "300", "400"):
            self.store.bind_thread("slack", "CDUMMY", thread, "dummy")
        self.source.client.api_call.return_value = {"messages": []}
        with self.assertLogs("slack_dispatch.ingest.slack_reconcile", level="INFO") as logs:
            self.source.catch_up()
        replies = [call.kwargs["params"]["ts"] for call in self.source.client.api_call.call_args_list
                   if call.args[0] == "conversations.replies"]
        self.assertEqual(replies, ["300"])
        self.assertEqual((self.source.summary["thread_candidates"], self.source.summary["thread_skipped_age"]), (3, 2))
        self.assertIn("thread_candidates=3 thread_skipped_age=2", logs.output[-1])

    def test_reply_callback_failure_keeps_replies_cursor(self):
        self.store.bind_thread("slack", "CDUMMY", "100", "dummy")
        self.source.client.api_call.side_effect = [{"messages": []}, {"messages": [dict(ts="999900", user="UOTHER")]}]
        self.source.on_event = Mock(side_effect=ValueError("private fixture text"))
        self.source.catch_up()
        self.assertIsNone(self.store.get_cursor("slack_replies", "CDUMMY"))

    def test_hydration_retry_and_cache(self):
        self.source.client.api_call.side_effect = [RuntimeError("private fixture text"),
                                                   {"messages": [dict(ts="100", user="UOTHER", text="root")]}]
        with self.assertRaises(RuntimeError):
            self.source.hydrate_root("CDUMMY", "100")
        self.assertEqual(self.source.pending_roots, {("CDUMMY", "100")})
        self.source.config = replace(self.source.config, replies=False)
        self.source.on_context = Mock()
        self.source.client.api_call.side_effect = lambda method, **kwargs: (
            {"messages": [dict(ts="100", user="UOTHER", text="root")]} if method == "conversations.replies" else {"messages": []})
        self.source.catch_up()
        root = self.source.on_context.call_args.args[0]
        self.assertEqual(root["origin"], "hydrate")
        self.assertEqual(root["text"], "root")
        count = self.source.client.api_call.call_count
        self.assertEqual(self.source.hydrate_root("CDUMMY", "100"), root)
        self.assertEqual(self.source.client.api_call.call_count, count)
        self.assertEqual(self.source.pending_roots, set())

    def test_hydration_skips_excluded_channel(self):
        self.source.excluded_channels = {"CEXCLUDED"}
        self.assertIsNone(self.source.hydrate_root("CEXCLUDED", "100"))
        self.source.client.api_call.assert_not_called()
        self.assertEqual(self.source.pending_roots, set())

    def test_hydration_gives_up_after_three_retries(self):
        self.source.config = replace(self.source.config, replies=False)
        def respond(method, **kwargs):
            if method == "conversations.replies":
                raise RuntimeError("private fixture text")
            return {"messages": []}
        self.source.client.api_call.side_effect = respond
        with self.assertRaises(RuntimeError):
            self.source.hydrate_root("CDUMMY", "100")
        with self.assertLogs("slack_dispatch.ingest.slack_reconcile", level="INFO") as logs:
            for _ in range(4):
                self.source.catch_up()
        fetches = [call for call in self.source.client.api_call.call_args_list
                   if call.args[0] == "conversations.replies"]
        self.assertEqual(len(fetches), 4)
        self.assertEqual(self.source.pending_roots, set())
        self.assertIsNone(self.source.hydrate_root("CDUMMY", "100"))
        self.assertEqual(len([line for line in logs.output if "Hydration abandoned" in line]), 1)
        self.assertIn("root=slack:CDUMMY:100 attempts=4", " ".join(logs.output))
        self.assertNotIn("private fixture text", " ".join(logs.output))

    def test_timeout_keeps_one_outstanding_request(self):
        done = Mock()
        done.wait.return_value = False
        self.source._hydration_jobs["CDUMMY", "100"] = {"done": done}
        with self.assertRaises(TimeoutError):
            self.source.hydrate_root("CDUMMY", "100")
        done.wait.assert_called_once_with(10)
        self.source.client.api_call.assert_not_called()
        self.assertIn(("CDUMMY", "100"), self.source.pending_roots)

    def test_blocks_edit_and_file_body(self):
        elements = [dict(type="text", text="hello "), dict(type="user", user_id="UOWNER"),
                    dict(type="channel", channel_id="CDUMMY"), dict(type="link", url="https://example.invalid"),
                    dict(type="emoji", name="eyes"), dict(type="line_break"), dict(type="unknown", text="omit")]
        message = dict(ts="100", user="UOTHER", text="fallback", blocks=[dict(type="rich_text", elements=[
            dict(type="rich_text_section", elements=elements)])])
        expected = "hello <@UOWNER><#CDUMMY>https://example.invalid:eyes:\n"
        original = normalize_slack_event(dict(event_id="EvFIRST", event=dict(message, type="message", channel="CDUMMY")), STAMP, store_body=True)
        self.assertEqual(original["text"], expected)
        mid = self.store.record_event(original).message_id
        message["blocks"][0]["elements"][0]["elements"] = [dict(type="text", text="edited")]
        edit = normalize_slack_event(dict(event_id="EvEDIT", event=dict(type="message", subtype="message_changed",
                                          channel="CDUMMY", message=message)), STAMP, store_body=True)
        self.store.record_event(edit)
        self.assertEqual(self.store.get_message(mid, include_body=True)["body"], "edited")
        self.assertEqual(self.store.get_message(mid)["edited_at"], STAMP)
        files = normalize_slack_event(dict(event=dict(type="message", channel="CDUMMY", ts="200",
                                      files=[dict(name="dummy.txt"), dict(title="dummy.pdf")])), STAMP, store_body=True)
        self.assertEqual(files["text"], "[file] dummy.txt\n[file] dummy.pdf")
        self.assertNotIn("text", normalize_slack_event(dict(event=message), STAMP) or {})

    def test_v1_origin_migration_retains_rows_and_is_idempotent(self):
        self.store._db.execute("ALTER TABLE messages DROP COLUMN origin")
        self.store._db.execute("UPDATE schema_version SET version=1")
        self.store.migrate()
        self.store.migrate()
        self.assertEqual(self.store._rows("SELECT version FROM schema_version"), [{"version": 2}])
        self.assertIn("origin", {row["name"] for row in self.store._rows("PRAGMA table_info(messages)")})


if __name__ == "__main__":
    unittest.main()
