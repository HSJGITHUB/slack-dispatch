"""P23 catch-up wakes: recent backfill wakes like a live post (dummy ids only)."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from slack_dispatch.config.loader import ConfigError, load_config, parse_config
from slack_dispatch.console.queries import parse_log_line, reconcile_summary
from slack_dispatch.ingest.slack_reconcile import SlackReconcileSource
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline, catchup_recent, lane_kind
from slack_dispatch.router.classifier import NullSuggester
from slack_dispatch.service import _ServiceReconcileSource
from tests.test_pipeline_integration import SAMPLE, event, fixture_config
import yaml

NOW = 10000.0
FIVE_MINUTES_AGO = "9700.000001"
TWO_HOURS_AGO = "2800.000001"


class CatchupPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dummy-catchup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = fixture_config(self.root)
        self.store = LedgerStore(self.config.ledger.path)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id, session_pointer=lane.session, paused=lane.paused)
        self.now = NOW
        self.clock = lambda: self.now
        self.lane, self.human = Mock(), Mock()
        self.notifiers = [FileInboxNotifier(self.config.inbox.root, self.store.get_message),
                          self.lane, self.human]
        self.pipeline = self.make_pipeline()

    def make_pipeline(self):
        return Pipeline(self.config, self.store, NullSuggester(), self.notifiers,
                        WakePolicyEngine(self.config.wake_policy, self.clock), self.clock)

    def with_wake_policy(self, **changes):
        self.config = replace(self.config, wake_policy=replace(self.config.wake_policy, **changes))
        self.pipeline = self.make_pipeline()

    def records(self, lane="support"):
        path = self.root / "inbox" / f"{lane}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def lane_deliveries(self):
        return self.store._rows("SELECT kind, lane, item_ids, status FROM delivery_log "
                                "WHERE purpose='lane' ORDER BY id")

    def backfill(self, number=1, ts=FIVE_MINUTES_AGO, **changes):
        return self.pipeline.handle_event(event(number, ts=ts, origin="reconcile", **changes))

    def offered(self):
        return patch.object(self.pipeline.wake_engine, "offer", wraps=self.pipeline.wake_engine.offer)

    def test_recent_backfill_gets_inbox_record_and_one_catchup_wake(self):
        with self.offered() as offer:
            result = self.backfill()
        self.assertEqual((result.state, result.lane), ("assigned", "support"))
        self.assertTrue(result.catchup)
        self.assertFalse(result.backfill)
        offer.assert_called_once()
        self.lane.notify.assert_called_once_with("support", [result.message_id])
        # The inbox record keeps kind backfill; the delivery log row says catchup.
        self.assertEqual([(row["id"], row["kind"]) for row in self.records()],
                         [(result.message_id, "backfill")])
        rows = self.lane_deliveries()
        self.assertEqual([(row["kind"], row["lane"], row["status"]) for row in rows],
                         [("catchup", "support", "sent")])
        self.assertEqual(json.loads(rows[0]["item_ids"]), [result.message_id])
        snapshot = self.pipeline.snapshot()
        self.assertEqual((snapshot["catchup_woken"], snapshot["backfill_silent"]), (1, 0))
        self.assertEqual(self.pipeline.catchup_ids, {})

    def test_two_hour_old_backfill_fills_inbox_only(self):
        with self.offered() as offer:
            result = self.backfill(ts=TWO_HOURS_AGO)
        offer.assert_not_called()
        self.lane.notify.assert_not_called()
        self.assertTrue(result.backfill)
        self.assertFalse(result.catchup)
        self.assertEqual([row["kind"] for row in self.records()], ["backfill"])
        self.assertEqual(self.lane_deliveries(), [])
        snapshot = self.pipeline.snapshot()
        self.assertEqual((snapshot["catchup_woken"], snapshot["backfill_silent"]), (0, 1))

    def test_catchup_minutes_zero_fills_inbox_only(self):
        self.with_wake_policy(catchup_minutes=0)
        with self.offered() as offer:
            result = self.backfill()
        offer.assert_not_called()
        self.lane.notify.assert_not_called()
        self.assertTrue(result.backfill)
        self.assertEqual([row["kind"] for row in self.records()], ["backfill"])

    def test_window_boundary_is_inclusive(self):
        self.with_wake_policy(catchup_minutes=5)
        self.assertTrue(self.backfill(ts="9700.000000").catchup)
        self.assertFalse(self.backfill(2, ts="9699.999999").catchup)

    def test_duplicate_socket_event_after_catchup_does_not_wake_again(self):
        first = self.backfill()
        self.lane.notify.assert_called_once()
        self.now += 120  # the socket event of the same message arrives after the restart
        with self.offered() as offer:
            duplicate = self.pipeline.handle_event(event(1, ts=FIVE_MINUTES_AGO, origin="socket",
                                                         event_id="EvDUMMYSOCKET1"))
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.message_id, first.message_id)
        offer.assert_not_called()
        self.pipeline.flush(self.now + 600)
        self.lane.notify.assert_called_once()
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len(self.lane_deliveries()), 1)

    def test_debounced_catchup_wake_is_logged_as_catchup_on_flush(self):
        first = self.backfill()
        second = self.backfill(2, ts="9701.000001")
        self.lane.notify.assert_called_once_with("support", [first.message_id])
        self.pipeline.flush(self.now + self.config.wake_policy.debounce_seconds)
        self.lane.notify.assert_called_with("support", [second.message_id])
        self.assertEqual([row["kind"] for row in self.lane_deliveries()], ["catchup", "catchup"])
        self.assertEqual(self.pipeline.catchup_ids, {})

    def test_storm_rule_counts_catchup_items_against_the_lane_budget(self):
        self.with_wake_policy(storm_per_hour=3, batch_seconds=10)
        results = [self.backfill(number, ts=f"97{number:02d}.000001") for number in range(1, 6)]
        self.assertTrue(all(result.catchup for result in results))
        self.assertEqual(len(self.pipeline.wake_engine.events["support"]), 5)
        self.assertIn("support", self.pipeline.wake_engine.storm)
        # One storm notice to the human queue (the third item), no wake for items 2 to 5 yet.
        self.human.notify.assert_called_once_with("human_queue", [results[2].message_id])
        self.lane.notify.assert_called_once_with("support", [results[0].message_id])
        # A live post right after still falls in the same storm batch.
        live = self.pipeline.handle_event(event(9, ts="9999.000001", origin="socket"))
        self.assertEqual(self.lane.notify.call_count, 1)
        self.now += 10
        self.pipeline.flush(self.now)
        batch = sorted([result.message_id for result in results[1:]] + [live.message_id])
        self.lane.notify.assert_called_with("support", batch)
        self.assertEqual(len(self.records()), 6)
        # A batch of catch-up items and a live item is logged as new.
        self.assertEqual([row["kind"] for row in self.lane_deliveries()], ["catchup", "new"])

    def test_storm_of_catchup_items_only_is_logged_as_catchup(self):
        self.with_wake_policy(storm_per_hour=2, batch_seconds=10)
        results = [self.backfill(number, ts=f"97{number:02d}.000001") for number in range(1, 4)]
        self.now += 10
        self.pipeline.flush(self.now)
        self.lane.notify.assert_called_with("support", sorted(r.message_id for r in results[1:]))
        self.assertEqual([row["kind"] for row in self.lane_deliveries()], ["catchup", "catchup"])

    def test_recent_backfill_to_human_queue_gets_its_notice(self):
        with self.offered() as offer:
            result = self.backfill(text="no tag")
        self.assertEqual(result.state, "human_pending")
        self.assertFalse(result.catchup)
        offer.assert_not_called()
        self.human.notify.assert_called_once_with("human_queue", [result.message_id])
        self.lane.notify.assert_not_called()

    def test_recent_backfill_with_attention_wakes_and_notifies_human(self):
        result = self.backfill(text="[support] <@U0000000093>")
        self.assertEqual(result.state, "assigned")
        self.assertTrue(result.catchup)
        self.lane.notify.assert_called_once_with("support", [result.message_id])
        self.human.notify.assert_called_once_with("human_queue", [result.message_id])

    def test_prehandled_recent_backfill_never_wakes(self):
        with self.offered() as offer:
            replied = self.backfill(prehandled_by="owner_reply:9800.000001")
            reacted = self.backfill(2, ts="9701.000001",
                                    reactions=[dict(name="white_check_mark",
                                                    users=[self.config.always_human.owner_user])])
        for result in (replied, reacted):
            self.assertEqual(result.state, "done")
            self.assertFalse(result.catchup)
            self.assertTrue(self.store.get_message(result.message_id)["note"].startswith("prehandled:"))
        offer.assert_not_called()
        self.lane.notify.assert_not_called()
        self.human.notify.assert_not_called()
        self.assertEqual(self.records(), [])

    def test_socket_event_is_not_counted_as_catchup(self):
        result = self.pipeline.handle_event(event(1, ts=FIVE_MINUTES_AGO, origin="socket"))
        self.assertFalse(result.catchup)
        self.assertNotIn("catchup_woken", self.pipeline.snapshot())
        self.assertEqual([row["kind"] for row in self.lane_deliveries()], ["new"])

    def test_wake_switch_off_skips_like_live_and_keeps_inbox(self):
        (self.root / "state").mkdir()
        (self.root / "state/wake.off").touch()
        result = self.backfill()
        self.assertTrue(result.catchup)
        self.lane.notify.assert_not_called()
        self.assertEqual([row["kind"] for row in self.records()], ["backfill"])


class CatchupHelperTests(unittest.TestCase):
    def config(self, minutes):
        return Mock(wake_policy=Mock(catchup_minutes=minutes))

    def test_recent_window_and_disable(self):
        self.assertTrue(catchup_recent(self.config(30), "1000.000001", 1000 + 1800))
        self.assertFalse(catchup_recent(self.config(30), "1000.000001", 1000 + 1801))
        self.assertTrue(catchup_recent(self.config(30), "1060.000000", 1000))  # clock skew
        self.assertFalse(catchup_recent(self.config(0), "1000.000001", 1000))
        self.assertFalse(catchup_recent(self.config(30), None, 1000))
        self.assertFalse(catchup_recent(self.config(30), "not-a-ts", 1000))
        # A config without the key (or an invalid value) uses the default of 30 minutes.
        self.assertTrue(catchup_recent(Mock(wake_policy=object()), "1000", 1000 + 1800))
        self.assertTrue(catchup_recent(self.config(True), "1000", 1000 + 1800))

    def test_lane_kind_mixes(self):
        rows = {"a": {"origin": "reconcile"}, "b": {"origin": "reconcile"}, "c": {"origin": "socket"},
                "d": {"origin": "reconcile", "route_reason": "reroute:dummy"}}
        store = Mock(get_message=lambda mid: rows[mid])
        self.assertEqual(lane_kind(store, ["a", "b"]), "backfill")
        self.assertEqual(lane_kind(store, ["a", "b"], {"a": None}), "catchup")
        self.assertEqual(lane_kind(store, ["a"], {"a": None}), "catchup")
        self.assertEqual(lane_kind(store, ["a", "c"], {"a": None}), "new")
        self.assertEqual(lane_kind(store, ["a", "d"], {"a": None}), "reroute")


class CatchupConfigTests(unittest.TestCase):
    def raw(self, **wake):
        data = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        data["wake_policy"].update(wake)
        return data

    def test_default_explicit_and_invalid_values(self):
        data = self.raw()
        data["wake_policy"].pop("catchup_minutes", None)
        self.assertEqual(parse_config(data).wake_policy.catchup_minutes, 30)
        self.assertEqual(parse_config(self.raw(catchup_minutes=0)).wake_policy.catchup_minutes, 0)
        self.assertEqual(parse_config(self.raw(catchup_minutes=45)).wake_policy.catchup_minutes, 45)
        for value in (-1, True, "30", 1.5, None):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse_config(self.raw(catchup_minutes=value))

    def test_sample_config_sets_thirty(self):
        self.assertEqual(load_config(SAMPLE).wake_policy.catchup_minutes, 30)


class CatchupReconcilePassTests(unittest.TestCase):
    """The reconcile pass summary line reports catchup_wakes=N."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dummy-catchup-pass-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = fixture_config(self.root)
        self.store = LedgerStore(":memory:", store_body=True)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id)
        self.now = 1000
        self.clock = lambda: self.now
        self.lane = Mock()
        notifiers = [FileInboxNotifier(self.config.inbox.root, self.store.get_message), self.lane, Mock()]
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(), notifiers,
                                 WakePolicyEngine(self.config.wake_policy, self.clock), self.clock)
        entry = replace(load_config(SAMPLE).sources[-1], store_body=True, replies=False)
        self.source = _ServiceReconcileSource(entry, self.pipeline.handle_event, "dummy.env", "DUMMY",
                                              self.store, excluded_channels=(), known_bot_users=())
        self.source.client = Mock()
        self.store.set_cursor("slack", "C0000000002", "500")

    def run_pass(self, *messages):
        self.source.client.api_call.return_value = {"messages": list(messages)}
        with self.assertLogs("slack_dispatch.ingest.slack_reconcile", level="INFO") as logs:
            self.source.catch_up()
        return [line for line in logs.output if "Reconcile pass " in line][-1]

    def test_pass_line_counts_catchup_wakes_and_silent_backfill(self):
        line = self.run_pass(dict(ts="900.000001", user="U0000000088", text="[support] dummy one"),
                             dict(ts="901.000001", user="U0000000089", text="[support] dummy two"))
        self.assertIn("backfill delivered 0 (no wake); catchup_wakes=2", line)
        self.assertEqual(self.source.summary["catchup_wakes"], 2)
        self.assertEqual(self.lane.notify.call_count, 2)
        self.now = 9000  # the next item is over two hours old
        line = self.run_pass(dict(ts="950.000001", user="U0000000088", text="[support] dummy three"))
        self.assertIn("backfill delivered 1 (no wake); catchup_wakes=0", line)
        snapshot = self.pipeline.snapshot()
        self.assertEqual((snapshot["catchup_woken"], snapshot["backfill_silent"]), (2, 1))

    def test_plain_source_line_and_console_summary(self):
        entry = replace(load_config(SAMPLE).sources[-1], store_body=True, replies=False)
        self.source = SlackReconcileSource(entry, self.pipeline.handle_event, "dummy.env", "DUMMY",
                                           self.store, excluded_channels=())
        self.source.client = Mock()
        line = self.run_pass(dict(ts="900.000001", user="U0000000088", text="[support] dummy one"))
        self.assertTrue(line.endswith("backfill delivered 0 (no wake); catchup_wakes=1"))
        message = line.split(":", 2)[2]
        parsed = parse_log_line("2026-01-01 00:00:00,000 INFO slack_dispatch.ingest.slack_reconcile "
                                + message)
        counts = reconcile_summary(parsed)["counts"]
        self.assertEqual((counts["backfill"], counts["catchup_wakes"], counts["roots"]), (0, 1, 1))


if __name__ == "__main__":
    unittest.main()
