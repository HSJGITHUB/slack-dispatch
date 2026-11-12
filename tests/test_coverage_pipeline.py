"""Offline owner policy, hydration, backfill and reconnect integration."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from slack_dispatch.config.loader import ConfigError, load_config, parse_config
from slack_dispatch.ingest.slack_reconcile import SlackReconcileSource
from slack_dispatch.ingest.slack_socket import SlackSocketSource
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline
from slack_dispatch.runtime.heartbeat import HeartbeatWriter, read_heartbeat
from slack_dispatch.service import CoverageMonitor
from tests.test_pipeline_integration import SAMPLE, event, fixture_config
import yaml


class CoveragePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dummy-coverage-")
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
        self.notifiers = [FileInboxNotifier(self.config.inbox.root, self.store.get_message), Mock(), Mock()]
        self.suggester = Mock()
        self.pipeline = Pipeline(self.config, self.store, self.suggester, self.notifiers,
                                 WakePolicyEngine(self.config.wake_policy, self.clock), self.clock)
        self.owner = self.config.always_human.owner_user

    def assert_quiet(self):
        self.suggester.suggest.assert_not_called()
        self.notifiers[1].notify.assert_not_called()
        self.notifiers[2].notify.assert_not_called()
        self.assertFalse((self.root / "inbox/support.jsonl").exists())
        self.assertEqual(self.store._rows("SELECT * FROM bindings"), [])
        self.assertEqual(self.store._rows("SELECT * FROM human_notices"), [])
        self.assertEqual(self.pipeline.snapshot()["routed"], 0)

    def test_owner_post_record_and_route_opt_out(self):
        result = self.pipeline.handle_event(event(user=self.owner))
        row = self.store.get_message(result.message_id, include_body=True)
        self.assertEqual((row["state"], row["route_reason"], row["note"]), ("done", "owner_post", "owner_post"))
        self.assertEqual(row["body"], event()["text"])
        self.assert_quiet()
        self.pipeline.config = replace(self.config, owner_signals=replace(self.config.owner_signals, posts="route"))
        self.assertEqual(self.pipeline.handle_event(event(2, user=self.owner)).state, "assigned")

    def test_unset_owner_is_inert(self):
        self.pipeline.config = replace(self.config, always_human=replace(self.config.always_human, owner_user=None))
        self.assertEqual(self.pipeline.handle_event(event(user=self.owner)).state, "assigned")

    def test_owner_reactions_transitions_other_and_removed_are_inert(self):
        first = self.pipeline.handle_event(event())
        def react(number, emoji, actor=None, kind="reaction_added"):
            return self.pipeline.handle_event(event(number, ts=event()["ts"], event_type=kind,
                       reaction=emoji, user=actor or self.owner))
        react(2, "eyes", "UOTHER")
        self.assertEqual(self.store.get_message(first.message_id)["state"], "assigned")
        react(3, "eyes")
        self.assertEqual(self.store.get_message(first.message_id)["state"], "acked")
        react(4, "white_check_mark")
        self.assertEqual(self.store.get_message(first.message_id)["note"], "owner_reaction:white_check_mark")
        react(5, "white_check_mark", kind="reaction_removed")
        react(6, "eyes")
        self.assertEqual(self.store.get_message(first.message_id)["state"], "done")
        self.assertEqual(len(self.store.list_reactions(first.message_id)), 3)
        for number, state in enumerate(("new", "human_pending", "excluded"), 10):
            self.store.set_state(first.message_id, state)
            react(number, "heavy_check_mark")
            self.assertEqual(self.store.get_message(first.message_id)["state"], "excluded" if state == "excluded" else "done")

    def test_prehandled_reaction_precedes_owner_reply(self):
        result = self.pipeline.handle_event(event(origin="reconcile", prehandled_by="owner_reply:2000",
                         reactions=[dict(name="white_check_mark", users=[self.owner])]))
        row = self.store.get_message(result.message_id, include_body=True)
        self.assertEqual(row["note"], "prehandled:reaction:white_check_mark")
        self.assertEqual(row["origin"], "reconcile")
        self.assertEqual(row["body"], event()["text"])
        self.assert_quiet()
        self.assertEqual(self.pipeline.snapshot()["prehandled"], 1)

    def test_prehandled_owner_reply_and_socket_inert(self):
        result = self.pipeline.handle_event(event(origin="reconcile", prehandled_by="owner_reply:2000"))
        self.assertEqual(self.store.get_message(result.message_id)["note"], "prehandled:owner_reply")
        self.assert_quiet()
        result = self.pipeline.handle_event(event(2, origin="socket", prehandled_by="owner_reply:2000"))
        self.assertEqual(result.state, "assigned")

    def test_backfill_inbox_kind_and_followup_override(self):
        self.pipeline.handle_event(event(origin="reconcile"))
        self.pipeline.handle_event(event(2, origin="reconcile", thread_ts=event()["ts"], is_thread_reply=True))
        rows = [json.loads(line) for line in (self.root / "inbox/support.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["kind"] for row in rows], ["backfill", "followup"])
        self.assertNotIn(event()["text"], json.dumps(rows))

    def test_backfill_fills_inbox_without_wake(self):
        self.store.set_cursor("slack", "C0000000002", "500")
        source = SlackReconcileSource(replace(load_config(SAMPLE).sources[-1], store_body=True, replies=False),
                                      self.pipeline.handle_event, "dummy.env", "DUMMY", self.store)
        source.client = Mock()
        source.client.api_call.return_value = {"messages": [
            dict(ts="900.000001", user="U0000000088", text="[support] private fixture text")]}
        with patch.object(self.pipeline.wake_engine, "offer", wraps=self.pipeline.wake_engine.offer) as offer, \
             self.assertLogs("slack_dispatch.ingest.slack_reconcile", level="INFO") as logs:
            source.catch_up()
            offer.assert_not_called()
            self.notifiers[1].notify.assert_not_called()
            rows = [json.loads(line) for line in (self.root / "inbox/support.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["kind"] for row in rows], ["backfill"])
            live = self.pipeline.handle_event(event(origin="socket"))
            self.assertFalse(live.backfill)
            offer.assert_called_once()
        self.notifiers[1].notify.assert_called_once()
        self.assertIn("backfill delivered 1 (no wake)", logs.output[-1])

    def test_excluded_channel_reply_skips_hydration(self):
        source = Mock(pending_roots=set())
        source.hydrate_root.return_value = None
        self.pipeline.reconcile_source = source
        self.pipeline.handle_event(event(channel="C0000000003", thread_ts="900", is_thread_reply=True, origin="socket"))
        source.hydrate_root.assert_not_called()
        self.pipeline.handle_event(event(2, thread_ts="900", is_thread_reply=True, origin="socket"))
        source.hydrate_root.assert_called_once_with("C0000000002", "900")

    def test_last_event_at_tracks_socket_events_only(self):
        self.pipeline.handle_event(event(origin="reconcile", received_at="2026-01-01T00:00:00+00:00"))
        self.pipeline.handle_event(event(2, received_at="2026-01-01T00:00:01+00:00"))
        self.assertIsNone(self.pipeline.last_event_at)
        self.pipeline.handle_event(event(3, origin="socket", received_at="2026-01-02T00:00:00+00:00"))
        self.pipeline.handle_event(event(4, origin="reconcile", received_at="2026-01-03T00:00:00+00:00"))
        self.assertEqual(self.pipeline.last_event_at, "2026-01-02T00:00:00+00:00")

    def test_hydration_context_once_without_routing_root(self):
        source = SlackReconcileSource(load_config(SAMPLE).sources[-1], self.pipeline.handle_event,
                                      "dummy.env", "DUMMY", self.store)
        source.config = replace(source.config, store_body=True)
        source.client = Mock()
        source.client.api_call.return_value = {"messages": [dict(ts="900", user="UOTHER", text="dummy root")]}
        self.pipeline.reconcile_source = source
        first = self.pipeline.handle_event(event(thread_ts="900", is_thread_reply=True, origin="socket"))
        self.pipeline.handle_event(event(2, thread_ts="900", is_thread_reply=True, origin="reconcile"))
        source.client.api_call.assert_called_once()
        root = self.store.get_message("slack:C0000000002:900", include_body=True)
        self.assertEqual((root["state"], root["route_reason"], root["note"], root["origin"]),
                         ("done", "context:root", "context", "hydrate"))
        self.assertEqual(root["body"], "dummy root")
        self.assertEqual(self.pipeline.snapshot()["hydrated"], 1)
        self.assertEqual(self.pipeline.snapshot()["routed"], 2)
        self.assertEqual(first.state, "assigned")

    def test_reconnect_queues_catchup_gap_and_degraded_health(self):
        configs = load_config(SAMPLE).sources
        socket = SlackSocketSource(configs[0], self.pipeline.handle_event, "dummy.env", "DUMMY")
        reconcile = SlackReconcileSource(configs[1], self.pipeline.handle_event, "dummy.env", "DUMMY", self.store)
        monitor = CoverageMonitor(self.store, self.pipeline, [socket, reconcile], self.clock, 300)
        socket._observe(True)
        socket._observe(False)
        self.assertFalse(monitor.poll())
        self.now += 301
        self.assertTrue(monitor.poll())
        writer = HeartbeatWriter(self.root / "heartbeat.json", clock=self.clock)
        degraded = monitor.poll()
        writer.write(ok=not degraded, status="degraded" if degraded else "running", started_at=monitor.started_at,
                     last_event_at=self.pipeline.last_event_at, gaps=monitor.gaps)
        self.assertEqual(read_heartbeat(self.root / "heartbeat.json")["status"], "degraded")
        self.assertFalse(read_heartbeat(self.root / "heartbeat.json")["ok"])
        socket._observe(True)
        self.assertFalse(reconcile._wake.is_set())
        self.assertFalse(monitor.poll())
        self.assertTrue(reconcile._wake.is_set())
        self.assertEqual(socket.reconnects, 1)
        self.assertEqual(monitor.gaps, 2)
        monitor.caught_up(monitor.stamp(self.now))
        gaps = self.store._rows("SELECT * FROM ingest_gaps ORDER BY id")
        self.assertEqual(gaps[1]["note"], "disconnect")
        self.assertIsNotNone(gaps[1]["resumed_at"])
        self.assertIsNotNone(gaps[1]["catchup_at"])

    def test_reconnect_runs_catchup_on_worker_and_completes_gap(self):
        import sys
        from types import SimpleNamespace
        configs = load_config(SAMPLE).sources
        socket = SlackSocketSource(configs[0], self.pipeline.handle_event, "dummy.env", "DUMMY")
        source = SlackReconcileSource(configs[1], self.pipeline.handle_event, "dummy.env", "DUMMY",
                                      self.store, clock=self.clock)
        monitor = CoverageMonitor(self.store, self.pipeline, [socket, source], self.clock, 300)
        completed = threading.Event()
        threads = []
        def caught_up(at):
            threads.append(threading.current_thread())
            monitor.caught_up(at)
            completed.set()
        source.on_catchup = caught_up
        with patch.dict(sys.modules, {"slack_sdk": SimpleNamespace(WebClient=Mock())}), patch(
                "slack_dispatch.ingest.slack_reconcile.load_token", return_value="dummy"):
            source.start()
            try:
                self.assertTrue(completed.wait(2))
                completed.clear()
                socket._observe(True)
                socket._observe(False)
                monitor.poll()
                self.now += 1
                socket._observe(True)
                monitor.poll()
                self.assertTrue(completed.wait(2))
                self.assertEqual(threads, [source._thread, source._thread])
                rows = self.store._rows("SELECT * FROM ingest_gaps ORDER BY id")
                self.assertEqual(len(rows), 2)
                self.assertIsNotNone(rows[-1]["catchup_at"])
            finally:
                source.stop()

    def test_recorded_later_owner_reply_prehandles_root(self):
        self.pipeline.handle_event(event(2, user=self.owner, thread_ts=event()["ts"], is_thread_reply=True))
        result = self.pipeline.handle_event(event(origin="reconcile"))
        self.assertEqual(self.store.get_message(result.message_id)["note"], "prehandled:owner_reply")
        self.assert_quiet()

    def test_config_defaults_validation_and_lookback_inheritance(self):
        raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        raw.pop("owner_signals", None)
        raw["sources"][-1].pop("replies_lookback_days", None)
        raw["sources"][-1]["lookback_days"] = 3
        cfg = parse_config(raw)
        self.assertEqual(cfg.owner_signals.posts, "record")
        self.assertEqual(cfg.sources[-1].replies_lookback_days, 3)
        self.assertEqual(cfg.service.gap_alert_seconds, 300)
        self.assertEqual(cfg.sources[-1].bound_threads_max_age_days, 14)
        raw["sources"][-1]["bound_threads_max_age_days"] = 0
        with self.assertRaises(ConfigError):
            parse_config(raw)
        raw["sources"][-1]["bound_threads_max_age_days"] = 30
        self.assertEqual(parse_config(raw).sources[-1].bound_threads_max_age_days, 30)
        for value in (dict(posts="bad"), dict(reactions_done="eyes"), dict(reactions_ack=[None])):
            raw["owner_signals"] = value
            with self.assertRaises(ConfigError):
                parse_config(raw)


if __name__ == "__main__":
    unittest.main()
