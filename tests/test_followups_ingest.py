"""P9 follow-ups: permanent reply error TTL and one-shot body rehydration."""
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

from slack_dispatch import service
from slack_dispatch.config.loader import ConfigError, load_config, parse_config
from slack_dispatch.config.schema import SourceConfig
from slack_dispatch.ingest.slack_reconcile import SlackReconcileSource
from slack_dispatch.ledger.store import LedgerStore
from tests.test_pipeline_integration import SAMPLE, fixture_config

RECONCILE_LOG = "slack_dispatch.ingest.slack_reconcile"
STAMP = "2026-01-01T00:00:00+00:00"
NOW = 1_000_000.0
DAY = 86400


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class PermanentErrorTtlTests(unittest.TestCase):
    """Item 4: a permanently failing parent is retried once per TTL."""

    def setUp(self):
        self.store = LedgerStore(":memory:", store_body=True)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        self.store.upsert_lane("dummy")
        self.store.set_cursor("slack", "CDUMMY", "400000")
        self.store.set_cursor("slack_replies", "CDUMMY", "999000")
        self.store.bind_thread("slack", "CDUMMY", "200", "dummy")
        self.clock = Clock(NOW)
        self.events = []
        config = SourceConfig("slack_reconcile", True, 3600, store_body=True,
                              permanent_error_ttl_seconds=100)
        self.source = SlackReconcileSource(config, self.receive, "dummy.env", "DUMMY", self.store,
                                           clock=self.clock)
        self.source.client = Mock()
        self.calls = []
        self.replies = []
        self.source.client.api_call.side_effect = self.respond

    def receive(self, event):
        self.events.append(event)
        return self.store.record_event(event)

    def respond(self, method, **kwargs):
        if method == "conversations.history":
            return {"messages": []}
        self.calls.append((self.clock.now, kwargs["params"]["ts"]))
        return self.replies.pop(0)

    def run_pass(self, offset):
        self.clock.now = NOW + offset
        self.source.catch_up()
        return self.source.summary

    def test_retry_once_after_ttl_debug_after_first_warning_and_clear_on_success(self):
        self.replies = [{"ok": False, "error": "not_in_channel"}] * 2 + [{"messages": []}] * 2
        with self.assertLogs(RECONCILE_LOG, level="DEBUG") as logs:
            summaries = [dict(self.run_pass(offset)) for offset in (0, 50, 99, 100, 150, 200, 250)]
        # Fetched at 0 (fails), skipped until the TTL, retried at 100 (fails), retried at 200 (ok).
        self.assertEqual([at - NOW for at, _ in self.calls], [0, 100, 200, 250])
        self.assertEqual([row["permanent_skipped"] for row in summaries], [1, 1, 1, 1, 1, 0, 0])
        self.assertTrue(all(row["failures"] == 0 for row in summaries))
        self.assertEqual(self.source.skipped_parents, {})
        warnings = [line for line in logs.output if line.startswith("WARNING")]
        debug = [line for line in logs.output if line.startswith("DEBUG")]
        self.assertEqual(len(warnings), 1)
        self.assertIn("channel=CDUMMY parent=200 error=not_in_channel", warnings[0])
        self.assertEqual(len(debug), 1)
        self.assertIn("still skipped channel=CDUMMY parent=200 error=not_in_channel", debug[0])

    def test_skipped_at_moves_with_each_failed_retry(self):
        self.replies = [{"ok": False, "error": "is_archived"}] * 2
        self.run_pass(0)
        self.assertEqual(self.source.skipped_parents, {("CDUMMY", "200"): NOW})
        self.run_pass(120)
        self.assertEqual(self.source.skipped_parents, {("CDUMMY", "200"): NOW + 120})
        self.run_pass(219)
        self.assertEqual(len(self.calls), 2)

    def test_transient_error_after_ttl_is_an_ordinary_failure(self):
        self.replies = [{"ok": False, "error": "not_in_channel"}, {"ok": False, "error": "internal_error"},
                        {"messages": []}]
        self.run_pass(0)
        summary = self.run_pass(100)
        self.assertEqual((summary["failures"], summary["permanent_skipped"]), (1, 0))
        self.assertEqual(self.source.skipped_parents, {})
        self.run_pass(101)
        self.assertEqual(len(self.calls), 3)

    def test_config_key_default_custom_and_invalid(self):
        raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(parse_config(raw).sources[1].permanent_error_ttl_seconds, 3600)
        raw["sources"][1].pop("permanent_error_ttl_seconds")
        self.assertEqual(parse_config(raw).sources[1].permanent_error_ttl_seconds, 3600)
        raw["sources"][1]["permanent_error_ttl_seconds"] = 60
        self.assertEqual(parse_config(raw).sources[1].permanent_error_ttl_seconds, 60)
        for invalid in (0, -1, True, "3600", 1.5):
            raw["sources"][1]["permanent_error_ttl_seconds"] = invalid
            with self.assertRaises(ConfigError):
                parse_config(raw)


class FakeSlack:
    """Answers bounded history/replies reads from a ts -> message map and records calls."""

    def __init__(self, clock, messages, errors=()):
        self.clock, self.messages, self.errors = clock, messages, set(errors)
        self.calls = []

    def api_call(self, method, http_verb=None, params=None):
        self.calls.append((self.clock(), method, dict(params)))
        if http_verb != "GET":
            raise AssertionError("read-only GET expected")
        ts = params["latest"]
        if ts in self.errors:
            return {"ok": False, "error": "not_in_channel"}
        rows = []
        if method == "conversations.replies":
            parent = self.messages.get(params["ts"])
            rows = [parent] if parent else []
        found = self.messages.get(ts)
        if found and found not in rows:
            rows.append(found)
        return {"ok": True, "messages": rows}


class RehydrateTests(unittest.TestCase):
    """Item 7: fill null bodies once through the reconcile client."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-rehydrate-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        base = fixture_config(self.root)
        self.config = replace(base, ledger=replace(base.ledger, store_body=True),
                              sources=load_config(SAMPLE).sources)
        self.store = LedgerStore(self.config.ledger.path, store_body=True)
        self.store.open()
        self.store.migrate()
        self.store.upsert_lane("support")
        self.clock = Clock(NOW + 3 * DAY)
        self.sleeps = []

    def tearDown(self):
        self.store.close()

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock.now += seconds

    def add(self, ts, *, channel="CDUMMY", thread=None, state="new", body=None, note=None):
        mid = f"slack:{channel}:{ts}"
        event = dict(source="slack", event_type="message", event_id="EvDUMMY" + ts.replace(".", "x") + channel,
                     channel=channel, ts=ts, thread_ts=thread, user="UDUMMY", received_at=STAMP)
        if body is not None:
            event["text"] = body
        self.store.record_event(event)
        if state != "new":
            self.store.set_state(mid, state)
        if note:
            self.store._update(mid, {"note": note})
        return mid

    def body(self, mid):
        self.store.open()
        return self.store.get_message(mid, include_body=True)["body"]

    def run_once(self, client, days=7):
        self.store.close()
        out = io.StringIO()
        code = service.run_rehydrate(self.config, days, client=client, clock=self.clock,
                                     sleep=self.sleep, stdout=out)
        return code, out.getvalue()

    def test_fills_roots_and_replies_and_skips_ineligible_rows(self):
        root = self.add(f"{NOW:.6f}")
        reply = self.add(f"{NOW + 1:.6f}", thread=f"{NOW:.6f}", state="assigned")
        acked = self.add(f"{NOW + 2:.6f}", state="acked")
        pending = self.add(f"{NOW + 3:.6f}", state="human_pending")
        done = self.add(f"{NOW + 4:.6f}", state="done")
        excluded = self.add(f"{NOW + 5:.6f}", channel="C0000000003")
        old = self.add(f"{NOW - 5 * DAY:.6f}")
        kept = self.add(f"{NOW + 6:.6f}", body="DUMMY_EXISTING")
        deleted = self.add(f"{NOW + 7:.6f}", note="message_deleted")
        missing = self.add(f"{NOW + 8:.6f}")
        failing = self.add(f"{NOW + 9:.6f}")
        secret = "DUMMY_SECRET_TEXT"
        blocks = [dict(type="rich_text", elements=[dict(type="rich_text_section", elements=[
            dict(type="text", text="hello "), dict(type="user", user_id="UDUMMY2")])])]
        messages = {f"{NOW:.6f}": dict(ts=f"{NOW:.6f}", text=secret, blocks=blocks),
                    f"{NOW + 1:.6f}": dict(ts=f"{NOW + 1:.6f}", text=secret + " reply"),
                    f"{NOW + 2:.6f}": dict(ts=f"{NOW + 2:.6f}", text=secret + " acked"),
                    f"{NOW + 3:.6f}": dict(ts=f"{NOW + 3:.6f}", files=[dict(name="dummy.txt")]),
                    f"{NOW + 4:.6f}": dict(ts=f"{NOW + 4:.6f}", text="done"),
                    f"{NOW + 5:.6f}": dict(ts=f"{NOW + 5:.6f}", text="excluded"),
                    f"{NOW + 7:.6f}": dict(ts=f"{NOW + 7:.6f}", text="deleted")}
        client = FakeSlack(self.clock, messages, errors={f"{NOW + 9:.6f}"})
        with self.assertLogs("slack_dispatch.service", level="INFO") as logs:
            code, out = self.run_once(client)
        self.assertEqual(code, 1)  # one fetch failure
        self.assertEqual(out.strip(), "rehydrate candidates=6 fetched=5 filled=4 missing=1 "
                                      "failures=1 excluded=1")
        self.assertEqual(self.body(root), "hello <@UDUMMY2>")
        self.assertEqual(self.body(reply), secret + " reply")
        self.assertEqual(self.body(acked), secret + " acked")
        self.assertEqual(self.body(pending), "[file] dummy.txt")
        for mid in (done, excluded, old, deleted, missing, failing):
            self.assertIsNone(self.body(mid), mid)
        self.assertEqual(self.body(kept), "DUMMY_EXISTING")
        self.assertNotIn(secret, out + "".join(logs.output))
        self.assertIn("candidates=6 fetched=5 filled=4 missing=1 failures=1 excluded=1",
                      logs.output[-1])
        methods = {call[2]["latest"]: (call[1], call[2]) for call in client.calls}
        self.assertNotIn(f"{NOW + 5:.6f}", methods)
        method, params = methods[f"{NOW + 1:.6f}"]
        self.assertEqual(method, "conversations.replies")
        self.assertEqual(params, dict(channel="CDUMMY", ts=f"{NOW:.6f}", latest=f"{NOW + 1:.6f}",
                                      oldest=f"{NOW + 1:.6f}", inclusive=True, limit=10))
        method, params = methods[f"{NOW:.6f}"]
        self.assertEqual(method, "conversations.history")
        self.assertEqual((params["latest"], params["oldest"], params["inclusive"]),
                         (f"{NOW:.6f}", f"{NOW:.6f}", True))

    def test_rate_is_at_most_forty_calls_per_minute(self):
        for number in range(45):
            self.add(f"{NOW + number:.6f}")
        client = FakeSlack(self.clock, {})
        code, out = self.run_once(client)
        self.assertEqual(code, 0)
        self.assertIn("candidates=45 fetched=45 filled=0 missing=45", out)
        times = [call[0] for call in client.calls]
        self.assertEqual(len(times), 45)
        for start in times:
            self.assertLessEqual(sum(start <= other < start + 60 for other in times), 40)
        self.assertTrue(all(seconds > 0 for seconds in self.sleeps))

    def test_refuses_without_body_storage_reconcile_source_or_ledger(self):
        client = FakeSlack(self.clock, {})
        cases = [replace(self.config, ledger=replace(self.config.ledger, store_body=False)),
                 replace(self.config, sources=()),
                 replace(self.config, ledger=replace(self.config.ledger,
                                                     path=str(self.root / "missing.sqlite3")))]
        for config in cases:
            with self.subTest(config=config.ledger), patch("sys.stderr", new_callable=io.StringIO):
                self.store.close()
                self.assertEqual(service.run_rehydrate(config, 7, client=client, clock=self.clock,
                                                       sleep=self.sleep, stdout=io.StringIO()), 2)
        self.assertEqual(client.calls, [])
        self.assertFalse((self.root / "missing.sqlite3").exists())

    def test_main_mode_is_exclusive_and_validates_days(self):
        with patch("sys.stderr", new_callable=io.StringIO), \
             patch.object(service, "load_config", return_value=self.config), \
             patch.object(service, "run") as run, \
             patch.object(service, "run_rehydrate", return_value=0) as rehydrate:
            for args in (["--once"], ["--dry-run"]):
                self.assertEqual(service.main(["--config", "dummy.yaml", "--rehydrate-bodies", "7", *args]), 2)
            for days in ("0", "-1", "x"):
                self.assertEqual(service.main(["--config", "dummy.yaml", "--rehydrate-bodies", days]), 2)
            self.assertEqual(service.main(["--config", "dummy.yaml", "--rehydrate-bodies", "7"]), 0)
        rehydrate.assert_called_once_with(self.config, 7)
        run.assert_not_called()

    def test_main_uses_the_reconcile_client_and_exits(self):
        mid = self.add(f"{NOW:.6f}")
        self.store.close()
        client = FakeSlack(self.clock, {f"{NOW:.6f}": dict(ts=f"{NOW:.6f}", text="dummy body")})

        def open_client(source):
            source.client = client
            return client

        out = io.StringIO()
        with patch.object(service, "load_config", return_value=self.config), \
             patch.object(SlackReconcileSource, "open_client", autospec=True, side_effect=open_client), \
             patch.object(service.time, "time", side_effect=self.clock), \
             patch.object(service.time, "sleep", side_effect=self.sleep), \
             patch("sys.stdout", out):
            self.assertEqual(service.main(["--config", "dummy.yaml", "--rehydrate-bodies", "7"]), 0)
        self.assertIn("filled=1", out.getvalue())
        self.assertEqual(self.body(mid), "dummy body")
        self.assertFalse((self.root / "state/service.lock").exists())

    def test_store_set_body_fills_only_missing_bodies(self):
        mid = self.add(f"{NOW:.6f}")
        self.assertTrue(self.store.set_body(mid, "dummy"))
        self.assertFalse(self.store.set_body(mid, "other"))
        self.assertEqual(self.body(mid), "dummy")
        plain = LedgerStore(":memory:")
        plain.open()
        plain.migrate()
        self.addCleanup(plain.close)
        self.assertFalse(plain.set_body(mid, "dummy"))


if __name__ == "__main__":
    unittest.main()
