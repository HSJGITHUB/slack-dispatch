import io
import tempfile
import unittest
from unittest.mock import patch

from slack_dispatch.inbox.cli import main
from slack_dispatch.inbox.files import append, read, record
from tests.fakes.fake_store import FakeStore


class CLITests(unittest.TestCase):
    def test_owned_close_preserves_exit_codes(self):
        cases = [(["queue"], None, 0, ""),
                 (["ack", "missing"], None, 3, "Message not found: missing\n"),
                 (["assign", self.mid, "missing"], None, 3, "Lane not found\n"),
                 (["queue"], ValueError("dummy"), 2, "Invalid input\n"),
                 (["queue"], RuntimeError("dummy"), 1, "Inbox operation failed\n")]
        for args, failure, expected, error in cases:
            for close_fails in (False, True):
                with self.subTest(args=args, failure=failure, close_fails=close_fails):
                    err = io.StringIO()
                    with patch("slack_dispatch.ledger.store.LedgerStore", return_value=self.store):
                        with patch.object(self.store, "migrate", side_effect=failure):
                            with patch.object(self.store, "close", side_effect=RuntimeError("dummy") if close_fails else None) as close:
                                code = main(["--ledger", "dummy.db", "--inbox-root", self.temp.name] + args,
                                            stdout=io.StringIO(), stderr=err)
                    self.assertEqual(code, 1 if close_fails else expected)
                    self.assertEqual(err.getvalue(), error or ("Inbox operation failed\n" if close_fails else ""))
                    close.assert_called_once_with()

    def test_owned_close_failure_still_overrides_base_exception(self):
        for close_fails in (False, True):
            with self.subTest(close_fails=close_fails):
                with patch("slack_dispatch.ledger.store.LedgerStore", return_value=self.store):
                    with patch.object(self.store, "migrate", side_effect=KeyboardInterrupt):
                        with patch.object(self.store, "close", side_effect=RuntimeError("dummy") if close_fails else None):
                            if close_fails:
                                self.assertEqual(main(["--ledger", "dummy.db", "queue"], stderr=io.StringIO()), 1)
                            else:
                                with self.assertRaises(KeyboardInterrupt):
                                    main(["--ledger", "dummy.db", "queue"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = FakeStore()
        self.store.upsert_lane("support")
        self.store.upsert_lane("other")
        self.mid = "slack:CDUMMY:1"
        self.store.messages[self.mid] = {"id": self.mid, "channel": "CDUMMY", "ts": "1", "lane": "support", "state": "human_pending", "text": "PRIVATE_BODY"}
        append(self.temp.name, "support", [record(self.store.messages[self.mid])])

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        code = main(["--inbox-root", self.temp.name] + args, store=self.store, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_inbox_privacy(self):
        code, out, _ = self.run_cli(["inbox", "support"])
        self.assertEqual(code, 0)
        self.assertIn(self.mid, out)
        self.assertNotIn("PRIVATE_BODY", out)
        self.assertIn("PRIVATE_BODY", self.run_cli(["inbox", "support", "--show-body"])[1])

    def test_ack_done(self):
        self.assertEqual(self.run_cli(["ack", self.mid])[0], 0)
        self.assertEqual(self.store.messages[self.mid]["state"], "acked")
        self.assertEqual(self.run_cli(["done", self.mid, "--note", "complete"])[0], 0)
        self.assertEqual(self.store.messages[self.mid]["note"], "complete")
        self.assertNotIn(self.mid, self.run_cli(["inbox", "support"])[1])
        self.assertIn(self.mid, self.run_cli(["inbox", "support", "--include-done"])[1])

    def test_reroute_assign_queue(self):
        self.store.set_suggestion(self.mid, "other", "dummy rule", "v1")
        out = self.run_cli(["queue"])[1]
        self.assertIn("other", out)
        self.assertNotIn("PRIVATE_BODY", out)
        self.assertIn("PRIVATE_BODY", self.run_cli(["queue", "--show-body"])[1])
        self.assertEqual(self.run_cli(["assign", self.mid, "other", "--note", "chosen"])[0], 0)
        self.assertEqual(self.store.messages[self.mid]["route_reason"], "human:cli")
        self.assertEqual(self.store.messages[self.mid]["note"], "chosen")
        self.assertEqual(self.run_cli(["reroute", self.mid, "--to", "support"])[0], 0)
        self.assertEqual(self.store.messages[self.mid]["route_reason"], "reroute:other")
        self.assertEqual(read(self.temp.name, "support")[-1]["kind"], "reroute")

    def test_claim_and_errors(self):
        self.assertEqual(self.run_cli(["claim", "support", "--session", "dummy-session"])[0], 0)
        with patch.dict("os.environ", {"SLACK_DISPATCH_SESSION": "dummy-env"}):
            self.assertEqual(self.run_cli(["claim", "support"])[0], 0)
        self.assertEqual(self.store.get_lane("support")["session_pointer"], "dummy-env")
        with patch.dict("os.environ", {"SLACK_DISPATCH_SESSION": "", "CLAUDE_SESSION_ID": "dummy-fallback"}):
            self.assertEqual(self.run_cli(["claim", "support"])[0], 0)
        for args in (["ack", "missing"], ["done", "missing"], ["reroute", "missing", "--to", "other"], ["assign", "missing", "other"]):
            self.assertEqual(self.run_cli(args)[0], 3)
        for args in ([], ["bad"], ["reroute", self.mid], ["claim", "../escape", "--session", "dummy"]):
            self.assertEqual(self.run_cli(args)[0], 2)
        with patch.dict("os.environ", {"SLACK_DISPATCH_SESSION": "", "CLAUDE_SESSION_ID": ""}):
            self.assertEqual(self.run_cli(["claim", "support"])[0], 2)

    def test_store_lifecycle_and_environment(self):
        with patch.dict("os.environ", {"SLACK_DISPATCH_LEDGER": "dummy.db", "SLACK_DISPATCH_INBOX": self.temp.name}):
            with patch("slack_dispatch.ledger.store.LedgerStore", return_value=self.store) as factory:
                self.assertEqual(main(["inbox", "support"], stdout=io.StringIO()), 0)
                factory.assert_called_once_with("dummy.db")

    def test_missing_destination_has_no_side_effects(self):
        before = dict(self.store.messages[self.mid])
        for args in (["assign", self.mid, "missing"],
                     ["reroute", self.mid, "--to", "missing"]):
            with self.subTest(args=args):
                code, out, err = self.run_cli(args)
                self.assertEqual(code, 3)
                self.assertEqual(out, "")
                self.assertEqual(err, "Lane not found\n")
                self.assertEqual(self.store.messages[self.mid], before)
                self.assertEqual(read(self.temp.name, "missing"), [])

    def test_unexpected_errors_are_sanitized(self):
        for method, args in (("get_message", ["ack", self.mid]),
                             ("get_lane", ["assign", self.mid, "other"]),
                             ("set_route", ["reroute", self.mid, "--to", "other"])):
            with self.subTest(method=method):
                with patch.object(self.store, method, side_effect=RuntimeError("PRIVATE_BODY\ntrace")):
                    code, _, err = self.run_cli(args)
                self.assertEqual(code, 1)
                self.assertEqual(err, "Inbox operation failed\n")

    def test_owned_store_errors_and_close_are_sanitized(self):
        for method in ("open", "migrate", "close"):
            with self.subTest(method=method):
                err = io.StringIO()
                with patch("slack_dispatch.ledger.store.LedgerStore", return_value=self.store):
                    with patch.object(self.store, method, side_effect=RuntimeError("PRIVATE_BODY")):
                        code = main(["--ledger", "dummy.db", "queue"],
                                    stdout=io.StringIO(), stderr=err)
                self.assertEqual(code, 1)
                self.assertEqual(err.getvalue(), "Inbox operation failed\n")
