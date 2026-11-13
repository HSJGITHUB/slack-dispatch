"""P12 item 5: wake requests from the CLI, the console and card decisions."""
from dataclasses import replace
import io
import json
import logging
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from slack_dispatch import service
from slack_dispatch.config.schema import Decisions
from slack_dispatch.console import actions
from slack_dispatch.console.queries import inbox_root
from slack_dispatch.decisions import sign
from slack_dispatch.inbox.cli import main as cli_main
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.dispatch_card import safe_name
from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline
from slack_dispatch.router.classifier import NullSuggester
from slack_dispatch.runtime import read_heartbeat
from tests.test_pipeline_integration import event, fixture_config

OWNER = "U0000000093"
KEY_HEX = "ab" * 32  # dummy signing key for tests, built without a hex literal


class ServiceLoopCase(unittest.TestCase):
    """Seed a ledger, then run the resident service for exactly one loop iteration."""

    def setUp(self):
        # The service configures the package logger (propagate off); restore it for later tests.
        logger = logging.getLogger("slack_dispatch")
        self.addCleanup(setattr, logger, "propagate", logger.propagate)
        self.addCleanup(logger.setLevel, logger.level)
        temp = tempfile.TemporaryDirectory(prefix="dispatch-wakes-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = fixture_config(self.root)
        self.notifiers = [Mock(), Mock(), Mock()]
        self.now = 1000.0
        self.store = LedgerStore(self.config.ledger.path)
        self.store.open()
        self.store.migrate()
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id)
        self.ids = []
        for number in (1, 2):
            mid = self.store.record_event(event(number)).message_id
            self.store.set_state(mid, "human_pending", note="none")
            self.ids.append(mid)

    def run_one_iteration(self):
        self.store.close()

        def tick(_):
            self.now += 1
            (self.root / "state").mkdir(exist_ok=True)
            (self.root / "state/stop.flag").write_text("dummy stop", encoding="utf-8")

        with patch.object(service, "load_config", return_value=self.config), \
                patch.object(service, "build_sources", return_value=[]), \
                patch.object(service, "build_notifiers", return_value=self.notifiers), \
                patch.object(service.time, "time", side_effect=lambda: self.now), \
                patch.object(service.time, "sleep", side_effect=tick):
            code = service.main(["--config", "dummy.yaml"])
        self.store.open()
        self.addCleanup(self.store.close)
        return code

    def lane_wakes(self):
        return [call.args for call in self.notifiers[1].notify.call_args_list]

    def requests(self):
        return self.store._rows("SELECT lane, message_id, source, consumed_at FROM wake_requests ORDER BY id")


class WakeRequestTests(ServiceLoopCase):
    def test_console_assign_then_one_loop_iteration_wakes_the_lane_once(self):
        result = actions.apply(self.store, inbox_root(self.config), "assign", [self.ids[0]],
                               lane="support", guard=actions.queue_guard)
        self.assertTrue(result[0]["ok"])
        self.assertEqual([(row["lane"], row["source"]) for row in self.requests()], [("support", "console")])
        self.assertEqual(self.run_one_iteration(), 0)
        self.assertEqual(self.lane_wakes(), [("support", [self.ids[0]])])
        self.assertTrue(all(row["consumed_at"] for row in self.requests()))
        self.notifiers[2].notify.assert_not_called()

    def test_duplicate_requests_are_consumed_with_one_wake(self):
        root = inbox_root(self.config)
        actions.assign(self.store, root, self.ids[0], "support")
        actions.assign(self.store, root, self.ids[0], "support", reason="human:cli")
        actions.assign(self.store, root, self.ids[1], "content")
        self.store.set_state(self.ids[1], "done", note="handled before the wake")
        self.assertEqual(len(self.requests()), 3)
        self.assertEqual(self.run_one_iteration(), 0)
        self.assertEqual(self.lane_wakes(), [("support", [self.ids[0]])])
        self.assertEqual([row["consumed_at"] is not None for row in self.requests()], [True] * 3)

    def test_card_decision_wakes_the_lane_and_reports_heartbeat_counters(self):
        cards = self.root / "cards_dir"
        key_file = self.root / "dispatch_hmac.env"
        key_file.write_text(f"DISPATCH_HMAC_KEY={KEY_HEX}\n", encoding="utf-8")
        self.config = replace(self.config, decisions=Decisions(dir=str(cards), hmac_key_file=str(key_file)))
        row = {"schema": "dispatch_decision/1", "message_id": self.ids[1], "lane": "support",
               "decided_by": OWNER, "decided_at": "2026-01-01T09:00:00+09:00", "card_ts": "1.000100"}
        row["hmac"] = sign(bytes.fromhex(KEY_HEX), row)
        (cards / "decisions").mkdir(parents=True)
        (cards / "decisions" / (safe_name(self.ids[1]) + ".json")).write_text(json.dumps(row), encoding="utf-8")
        self.assertEqual(self.run_one_iteration(), 0)
        self.assertEqual(self.store.get_message(self.ids[1])["route_reason"], "human:card")
        self.assertEqual(self.lane_wakes(), [("support", [self.ids[1]])])
        counters = read_heartbeat(self.root / "state/heartbeat.json")["counters"]
        self.assertEqual((counters["card_decisions"], counters["card_decisions_rejected"]), (1, 0))
        self.assertEqual(len(list((cards / "decisions" / "done").glob("*.json"))), 1)


class NoRequestTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-wakes-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = fixture_config(self.root)
        self.store = LedgerStore(self.config.ledger.path)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id)

    def test_socket_and_backfill_deliveries_and_cutover_create_no_requests(self):
        notifiers = [FileInboxNotifier(self.config.inbox.root, self.store.get_message), Mock(), Mock()]
        pipeline = Pipeline(self.config, self.store, NullSuggester(), notifiers,
                            WakePolicyEngine(self.config.wake_policy, lambda: 1000.0), lambda: 1000.0)
        pipeline.handle_event(event(1, text="[support] socket item", origin="socket"))
        pipeline.handle_event(event(2, text="[support] backfill item", origin="reconcile"))
        out = io.StringIO()
        code = cli_main(["--inbox-root", self.config.inbox.root, "cutover", "support", "--apply"],
                        store=self.store, stdout=out, stderr=io.StringIO())
        self.assertEqual(code, 0)
        self.assertIn("redelivered=2", out.getvalue())
        self.assertEqual(self.store.pending_wake_requests(), [])

    def test_consume_marks_requests_and_skips_items_owned_elsewhere(self):
        mid = self.store.record_event(event(1)).message_id
        self.store.set_route(mid, "content", "human:cli")
        self.store.request_wake("support", mid, "cli")
        pipeline = Mock(lock=threading.RLock())
        self.assertEqual(service.consume_wake_requests(pipeline, self.store), 0)
        pipeline.wake_engine.offer.assert_not_called()
        self.assertEqual(self.store.pending_wake_requests(), [])


if __name__ == "__main__":
    unittest.main()
