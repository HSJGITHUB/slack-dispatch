"""Offline service lifecycle tests. No source startup reaches a real SDK."""
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from slack_dispatch import service
from slack_dispatch.config.loader import load_config
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.runtime import SingleInstanceLock, read_heartbeat
from tests.test_pipeline_integration import SAMPLE, event, fixture_config


class FakeSource:
    failures = 0

    def __init__(self, config, on_event, events=()):
        self.config, self.on_event, self.events = config, on_event, events
        self.started = self.stopped = False

    def start(self):
        self.started = True
        for item in self.events:
            self.on_event(item)

    def stop(self):
        self.stopped = True


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dispatch-service-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = fixture_config(self.root)
        self.config = replace(self.config, sources=load_config(SAMPLE).sources)
        self.now = 1000.0
        self.created = []
        self.notifiers = [Mock(), Mock(), Mock()]

    def factory(self, config, on_event, store):
        self.factory_config = config
        self.created = [FakeSource(entry, on_event, [event(), event(2)])
                        for entry in config.sources if entry.enabled]
        return self.created

    def invoke(self, *args, factory=None, sleep=None):
        with patch.object(service, "load_config", return_value=self.config), \
             patch.object(service, "build_sources", side_effect=factory or self.factory), \
             patch.object(service, "build_notifiers", return_value=self.notifiers), \
             patch.object(service.time, "time", side_effect=lambda: self.now), \
             patch.object(service.time, "sleep", side_effect=sleep or (lambda _: None)):
            return service.main(["--config", "dummy.yaml", *args])

    def test_once_starts_reconcile_only_and_writes_heartbeat(self):
        self.assertEqual(self.invoke("--once"), 0)
        self.assertEqual([entry.kind for entry in self.factory_config.sources], ["slack_reconcile"])
        self.assertTrue(all(source.started and source.stopped for source in self.created))
        heartbeat = read_heartbeat(self.root / "state/heartbeat.json")
        self.assertEqual(heartbeat["status"], "stopped")
        self.assertEqual(set(heartbeat["counters"]),
                         {"events", "routed", "human_pending", "delivered", "failures", "duplicates", "prehandled", "hydrated"})
        self.assertEqual(heartbeat["counters"]["events"], 2)
        self.assertEqual(heartbeat["counters"]["routed"], 2)
        self.assertEqual(heartbeat["counters"]["failures"], 0)
        with SingleInstanceLock(self.root / "state/service.lock"):
            pass

    def test_resident_schedules_flush_timers_heartbeat_and_archives_stop(self):
        observations = []
        def tick(_):
            self.now += 1
            hb = read_heartbeat(self.root / "state/heartbeat.json")
            observations.append(hb["ts"])
            if self.now >= 1091:
                (self.root / "state/stop.flag").write_text("dummy stop", encoding="utf-8")
        with patch.object(service.TimerSweep, "run_once", return_value=0) as sweep:
            self.assertEqual(self.invoke(sleep=tick), 0)
        self.assertEqual(sweep.call_count, 1)
        self.assertGreaterEqual(len(set(observations)), 4)
        self.assertTrue(all(source.started and source.stopped for source in self.created))
        self.assertEqual(self.notifiers[1].notify.call_count, 2)
        self.assertFalse((self.root / "state/stop.flag").exists())
        self.assertEqual(len(list((self.root / "state/done").glob("*_stop.flag"))), 1)

    def test_config_error_and_second_instance_exit_codes(self):
        with patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(service.main(["--config", str(self.root / "missing.yaml")]), 2)
            with SingleInstanceLock(self.root / "state/service.lock"):
                self.assertEqual(self.invoke("--once"), 3)
        self.assertEqual(self.created, [])

    def test_dry_run_calls_no_real_notifier_but_routes_and_exports(self):
        def factory(config, on_event, store):
            self.created = [FakeSource(config.sources[0], on_event,
                                       [event(), event(2, text="unmatched")])]
            return self.created
        self.assertEqual(self.invoke("--once", "--dry-run", factory=factory), 0)
        for notifier in self.notifiers:
            notifier.notify.assert_not_called()
        self.assertTrue((self.root / "state/human_queue.jsonl").exists())
        store = LedgerStore(self.config.ledger.path)
        store.open()
        try:
            self.assertEqual(len(store.list_messages(state="assigned")), 1)
            self.assertEqual(len(store.list_messages(state="human_pending")), 1)
            self.assertEqual(store._rows("SELECT * FROM service_deliveries"), [])
            self.assertEqual(store._rows("SELECT * FROM timer_events"), [])
        finally:
            store.close()
        self.assertFalse((self.root / "inbox").exists())
        log = (self.root / "logs/service.log").read_text(encoding="utf-8")
        self.assertIn("Dry-run notification", log)
        self.assertNotIn("private fixture text", log)

    def test_once_config_flag_and_transient_text_policy(self):
        self.config = replace(self.config, service=replace(self.config.service, once=True))
        self.assertEqual(self.invoke(), 0)
        self.assertTrue(self.created[0].config.store_body)
        store = LedgerStore(self.config.ledger.path)
        store.open()
        try:
            self.assertTrue(all(row["body"] is None for row in store._rows("SELECT body FROM messages")))
        finally:
            store.close()

    def test_loop_exception_is_counted_sanitized_and_loop_continues(self):
        def tick(_):
            self.now += 30
            if self.now >= 1090:
                (self.root / "state/stop.flag").touch()
        with patch.object(service.TimerSweep, "run_once", side_effect=RuntimeError("private fixture text")):
            self.assertEqual(self.invoke(sleep=tick), 0)
        hb = read_heartbeat(self.root / "state/heartbeat.json")
        self.assertGreater(hb["counters"]["failures"], 0)
        self.assertFalse(hb["ok"])
        self.assertNotIn("private fixture text", (self.root / "logs/service.log").read_text())
        self.assertTrue(all(source.stopped for source in self.created))

    def test_source_start_failure_still_stops_and_unlocks(self):
        def factory(config, on_event, store):
            source = FakeSource(config.sources[0], on_event)
            source.start = Mock(side_effect=RuntimeError("private fixture text"))
            self.created = [source]
            return self.created
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(self.invoke("--once", factory=factory), 1)
        self.assertEqual(stderr.getvalue(), "Source start failed, see service.log\n")
        self.assertTrue(self.created[0].stopped)
        with SingleInstanceLock(self.root / "state/service.lock"):
            pass

    def test_existing_session_claim_is_preserved(self):
        store = LedgerStore(self.config.ledger.path)
        store.open()
        store.migrate()
        store.upsert_lane("support", session_pointer="dummy-new-session")
        store.close()
        self.assertEqual(self.invoke("--once"), 0)
        store.open()
        try:
            self.assertEqual(store.get_lane("support")["session_pointer"], "dummy-new-session")
        finally:
            store.close()

    def test_service_state_directory_controls_wake_switch(self):
        # P9: service.state_dir governs when wake_policy.state_dir is unset (default `state`);
        # a non-default wake_policy.state_dir wins (see test_followups_wake_dir).
        self.config = replace(self.config, wake_policy=replace(self.config.wake_policy,
                                                              state_dir="state"))
        (self.root / "state").mkdir()
        (self.root / "state/wake.off").touch()
        self.assertEqual(self.invoke("--once"), 0)
        self.notifiers[0].notify.assert_called()
        self.notifiers[1].notify.assert_not_called()

    def test_real_source_construction_and_once_gate_without_startup(self):
        store = LedgerStore(self.config.ledger.path)
        sources = service.build_service_sources(self.config, Mock(), store, once=True)
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertIsInstance(source, service._OnceReconcileSource)
        self.assertTrue(source.config.store_body)
        self.assertFalse(store.store_body)
        with patch.object(service.SlackReconcileSource, "catch_up", return_value=None) as catch:
            source.catch_up()
        catch.assert_called_once()
        self.assertTrue(source._stop.is_set())

    def test_cli_module_delegates_and_help_is_offline(self):
        import runpy
        with patch.object(service, "main", return_value=7) as main:
            with self.assertRaises(SystemExit) as result:
                runpy.run_module("slack_dispatch", run_name="__main__")
        self.assertEqual(result.exception.code, 7)
        main.assert_called_once_with()
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(service.main(["--help"]), 0)

    def test_once_inherited_start_never_runs_interval_pass(self):
        import sys
        from types import SimpleNamespace
        store = LedgerStore(self.config.ledger.path)
        source = service.build_service_sources(self.config, Mock(), store, once=True)[0]
        sdk = SimpleNamespace(WebClient=Mock())
        with patch.dict(sys.modules, {"slack_sdk": sdk}), \
             patch("slack_dispatch.ingest.slack_reconcile.load_token", return_value="dummy"), \
             patch.object(service.SlackReconcileSource, "catch_up") as catch:
            try:
                source.start()
                source._thread.join(timeout=1)
                self.assertFalse(source._thread.is_alive())
                catch.assert_called_once()
            finally:
                source.stop()

    def test_resident_heartbeat_does_not_wait_for_first_reconcile_pass(self):
        import sys
        import threading
        from types import SimpleNamespace
        from slack_dispatch.ingest import build_sources
        self.config = replace(self.config, sources=(self.config.sources[-1],))
        heartbeat = self.root / "state/heartbeat.json"
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        seen = {}
        def slow_pass(source):
            seen["heartbeat_at_entry"] = heartbeat.exists()
            entered.set()
            release.wait(5)
            finished.set()
        observations = []
        def tick(_):
            self.now += 1
            observations.append((entered.wait(2), finished.is_set(), read_heartbeat(heartbeat)))
            release.set()
            (self.root / "state/stop.flag").touch()
        with patch.dict(sys.modules, {"slack_sdk": SimpleNamespace(WebClient=Mock())}), \
             patch("slack_dispatch.ingest.slack_reconcile.load_token", return_value="dummy"), \
             patch.object(service.SlackReconcileSource, "catch_up", slow_pass):
            self.assertEqual(self.invoke(factory=build_sources, sleep=tick), 0)
        self.assertTrue(seen["heartbeat_at_entry"])
        self.assertEqual(len(observations), 1)
        was_entered, was_finished, beat = observations[0]
        self.assertTrue(was_entered)
        self.assertFalse(was_finished)
        self.assertEqual((beat["status"], beat["ok"]), ("running", True))
        self.assertTrue(finished.is_set())
        self.assertFalse(service._ServiceReconcileSource.wait_for_first_pass)
        self.assertTrue(service._OnceReconcileSource.wait_for_first_pass)

    def test_resident_all_sources_fail_exits_without_entering_loop(self):
        def factory(config, on_event, store):
            self.created = [FakeSource(config.sources[0], on_event) for _ in range(2)]
            for source in self.created:
                source.start = Mock(side_effect=RuntimeError("private fixture text"))
            return self.created
        sleep = Mock(side_effect=lambda _: (self.root / "state/stop.flag").touch())
        with patch("sys.stderr", new_callable=io.StringIO) as stderr, \
             patch.object(service.TimerSweep, "run_once") as sweep:
            self.assertEqual(self.invoke(factory=factory, sleep=sleep), 1)
        sleep.assert_not_called()
        sweep.assert_not_called()
        self.assertEqual(stderr.getvalue(), "Source start failed, see service.log\n")
        self.assertTrue(all(source.stopped for source in self.created))
        hb = read_heartbeat(self.root / "state/heartbeat.json")
        self.assertFalse(hb["ok"])
        self.assertEqual(hb["status"], "stopped")
        self.assertEqual(hb["counters"]["failures"], 2)
        log = (self.root / "logs/service.log").read_text(encoding="utf-8")
        self.assertIn("source_start", log)
        self.assertNotIn("private fixture text", log)
        with SingleInstanceLock(self.root / "state/service.lock"):
            pass

    def test_resident_partial_source_failure_keeps_running_with_unhealthy_heartbeat(self):
        def factory(config, on_event, store):
            bad = FakeSource(config.sources[0], on_event)
            bad.start = Mock(side_effect=RuntimeError("private fixture text"))
            good = FakeSource(config.sources[-1], on_event, [event()])
            self.created = [bad, good]
            return self.created
        observations = []
        def tick(_):
            observations.append(read_heartbeat(self.root / "state/heartbeat.json"))
            (self.root / "state/stop.flag").touch()
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(self.invoke(factory=factory, sleep=tick), 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["status"], "running")
        self.assertFalse(observations[0]["ok"])
        self.assertEqual(observations[0]["counters"]["events"], 1)
        self.assertEqual(observations[0]["counters"]["failures"], 1)
        self.assertTrue(self.created[1].started)
        self.assertTrue(all(source.stopped for source in self.created))
        log = (self.root / "logs/service.log").read_text(encoding="utf-8")
        self.assertIn("source_start", log)
        self.assertNotIn("private fixture text", log)

    def test_once_partial_source_start_failure_prints_one_fixed_line(self):
        def factory(config, on_event, store):
            good = FakeSource(config.sources[0], on_event, [event()])
            bad = FakeSource(config.sources[0], on_event)
            bad.start = Mock(side_effect=RuntimeError("private fixture text"))
            self.created = [good, bad]
            return self.created
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(self.invoke("--once", factory=factory), 1)
        self.assertEqual(stderr.getvalue(), "Source start failed, see service.log\n")
        self.assertTrue(self.created[0].started)
        self.assertTrue(all(source.stopped for source in self.created))

    def test_once_counted_source_failure_prints_one_fixed_line(self):
        def factory(config, on_event, store):
            source = FakeSource(config.sources[0], on_event)
            source.failures = 1
            self.created = [source]
            return self.created
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(self.invoke("--once", factory=factory), 1)
        self.assertEqual(stderr.getvalue(), "Source start failed, see service.log\n")
        self.assertTrue(self.created[0].started and self.created[0].stopped)
        self.assertFalse(read_heartbeat(self.root / "state/heartbeat.json")["ok"])
