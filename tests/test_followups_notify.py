"""P9 follow-ups: human-queue target, not-live logging throttle, wake switch path, lane ids."""
from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from slack_dispatch import service
from slack_dispatch.config.schema import HumanQueue
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify import build_notifiers, human_queue_target, wake_switch_path
from slack_dispatch.notify.file_drop import FileDropNotifier
from slack_dispatch.notify.session_message import SessionMessageNotifier
from slack_dispatch.notify.session_resolve import SessionResolver
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline, can_notify
from slack_dispatch.router.classifier import NullSuggester
from slack_dispatch.timers import TimerSweep
from tests.test_pipeline_integration import event, fixture_config
from tests.test_service_integration import FakeSource

ID_A = "0000aaaa-0000-4000-8000-000000000001"
ID_B = "0000aaaa-0000-4000-8000-000000000002"
ID_D = "2222cccc-0000-4000-8000-000000000004"
RESOLVE_LOG = "slack_dispatch.notify.session_resolve"


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class TempRoot(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p9-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def open_store(self, config):
        store = LedgerStore(config.ledger.path)
        store.open()
        store.migrate()
        self.addCleanup(store.close)
        for lane in config.lanes:
            store.upsert_lane(lane.id)
        return store


class HumanQueueTargetTests(TempRoot):
    """Item 1: the operator target never depends on a lane named human_queue."""

    def build(self, template=None, **config_changes):
        base = fixture_config(self.root)
        human = replace(base.notify_adapters[-1], kind="session_message", purpose="human_queue",
                        path=None, dry_run=False,
                        command_template=("dummy-cli", "--target", "{target}", "--message", "{message}"),
                        template=template if template is not None else {})
        self.config = replace(base, notify_adapters=(base.notify_adapters[0], human), **config_changes)
        self.store = self.open_store(self.config)
        self.notifiers = build_notifiers(self.config, self.store)
        self.runner = Mock(return_value=SimpleNamespace(returncode=0))
        self.notifiers[1].runner = self.runner
        self.clock = Clock()
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(), self.notifiers,
                                 WakePolicyEngine(self.config.wake_policy, self.clock), self.clock)

    def targets(self):
        return [call.args[0][2] for call in self.runner.call_args_list]

    def test_delivers_without_human_queue_lane_and_renders_configured_target(self):
        self.build({"target": "dummy-operator-session"})
        self.assertIsNone(self.store.get_lane("human_queue"))
        result = self.pipeline.handle_event(event(text="no tag"))
        self.assertEqual(result.state, "human_pending")
        self.assertEqual(self.targets(), ["dummy-operator-session"])
        self.assertIn(result.message_id, self.runner.call_args.args[0][4])
        self.assertIsNone(self.store.get_lane("human_queue"))

    def test_default_target_is_operator(self):
        self.build()
        self.pipeline.handle_event(event(text="no tag"))
        self.assertEqual(self.targets(), ["operator"])
        for template in (None, {}, {"target": ""}, {"lane": "{lane}"}):
            self.assertEqual(human_queue_target(SimpleNamespace(template=template)), "operator")

    def test_no_pause_session_or_wake_switch_applies(self):
        self.build({"target": "dummy-operator-session"},
                   wake_policy=replace(fixture_config(self.root).wake_policy, enabled=False))
        (self.root / "state").mkdir()
        (self.root / "state/wake.off").touch()
        # A leftover pseudo-lane, paused and claimed, is ignored for the human queue.
        self.store.upsert_lane("human_queue", session_pointer=ID_D, paused=True)
        self.pipeline.handle_event(event(text="no tag"))
        self.assertEqual(self.targets(), ["dummy-operator-session"])
        spec = self.config.notify_adapters[1]
        self.assertTrue(can_notify(self.config, self.store, spec, "human_queue", self.notifiers[1]))
        self.assertEqual(self.notifiers[1].session_resolver.skipped_no_session, 0)

    def test_timer_notice_uses_operator_target(self):
        self.build({"target": "dummy-operator-session"})
        mid = self.store.record_event(event()).message_id
        sweep = TimerSweep(self.config, self.store, self.notifiers, self.clock,
                           human=self.pipeline.human)
        unassigned = 1000 + self.config.timers.unassigned_seconds
        self.assertEqual(sweep.run_once(unassigned), 1)
        self.assertEqual(self.targets(), ["dummy-operator-session"])
        self.assertIn(mid, self.runner.call_args.args[0][4])

    def test_template_target_must_be_a_string(self):
        with self.assertRaises(ValueError):
            SessionMessageNotifier(["dummy"], lambda lane: "x", template={"target": 7})


class LaneIdLineTests(TempRoot):
    """Item 8: digests and immediate notices end with the configured lane ids."""

    def setUp(self):
        super().setUp()
        self.config = replace(fixture_config(self.root), human_queue=HumanQueue(digest_seconds=10))
        self.store = self.open_store(self.config)
        self.clock = Clock()
        self.notifiers = [Mock(), Mock(), FileDropNotifier(self.root / "human", {"ids": "{ids}"})]
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(), self.notifiers,
                                 WakePolicyEngine(self.config.wake_policy, self.clock), self.clock)

    def bodies(self):
        return [json.loads(path.read_text(encoding="utf-8"))["body"]
                for path in sorted((self.root / "human").glob("*.json"))]

    def test_digest_and_immediate_notice_end_with_lane_ids(self):
        self.pipeline.handle_event(event(user="U0000000091", text="plain"))  # immediate attention
        self.pipeline.handle_event(event(2, text="plain"))
        self.pipeline.flush(1010)
        bodies = self.bodies()
        self.assertEqual(len(bodies), 2)
        for body in bodies:
            lines = body.splitlines()
            self.assertIn("inbox.cli assign ID LANE", lines[-2])
            self.assertEqual(lines[-1], "Lane ids for assign: owner, support, content")
            self.assertNotIn("human_queue", lines[-1])


class NotLiveLoggingTests(unittest.TestCase):
    """Item 2: transitions and a 30 minute reminder per lane, distinct skip pairs."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.clock = Clock()
        self.resolver = SessionResolver(str(self.dir), clock=self.clock)

    def write(self, name, session_id, session_name):
        (self.dir / name).write_text(json.dumps({"sessionId": session_id, "name": session_name}),
                                     encoding="utf-8")

    def test_not_live_transitions_and_reminder(self):
        with self.assertLogs(RESOLVE_LOG, level="INFO") as logs:
            for _ in range(5):
                self.assertIsNone(self.resolver.resolve_target("support", ID_D))
        self.assertEqual(logs.output, ["INFO:%s:session not live: lane support" % RESOLVE_LOG])
        self.clock.now += 1799
        with self.assertNoLogs(RESOLVE_LOG, level="DEBUG"):
            self.resolver.resolve_target("support", ID_D)
        self.clock.now += 1
        with self.assertLogs(RESOLVE_LOG, level="INFO") as logs:
            self.resolver.resolve_target("support", ID_D)
            self.resolver.resolve_target("support", ID_D)
            self.resolver.resolve_target("content", ID_D)
        self.assertEqual(len(logs.output), 2)
        self.assertIn("session not live: lane support (reminder)", logs.output[0])
        self.assertIn("session not live: lane content", logs.output[1])
        self.write("1.json", ID_D, "dummy-delta")
        self.clock.now += 30  # scan cache expiry
        with self.assertLogs(RESOLVE_LOG, level="INFO") as logs:
            self.assertEqual(self.resolver.resolve_target("support", ID_D), "dummy-delta")
            self.assertEqual(self.resolver.resolve_target("support", ID_D), "dummy-delta")
        self.assertEqual(logs.output, ["INFO:%s:session live again: lane support" % RESOLVE_LOG])
        (self.dir / "1.json").rename(self.dir / "1.json.moved")
        self.clock.now += 30
        with self.assertLogs(RESOLVE_LOG, level="INFO") as logs:
            self.resolver.resolve_target("support", ID_D)
        self.assertIn("session not live: lane support", logs.output[0])
        self.assertNotIn(ID_D, str(logs.output))

    def test_ambiguous_on_first_sight_then_reminder(self):
        self.write("1.json", ID_A, "dummy-alpha")
        self.write("2.json", ID_B, "dummy-beta")
        with self.assertLogs(RESOLVE_LOG, level="INFO") as logs:
            for _ in range(3):
                self.assertIsNone(self.resolver.resolve_target("support", "0000aaaa"))
        self.assertEqual(len(logs.output), 1)
        self.assertTrue(logs.output[0].startswith("WARNING:"))
        self.assertIn("ambiguous session id: lane support matches 2 live sessions", logs.output[0])
        self.clock.now += 1800
        with self.assertLogs(RESOLVE_LOG, level="INFO") as logs:
            self.resolver.resolve_target("support", "0000aaaa")
        self.assertIn("(reminder)", logs.output[0])
        self.assertEqual(self.resolver.skipped_no_session, 0)

    def test_skipped_counts_distinct_lane_message_pairs(self):
        for _ in range(3):
            self.resolver.resolve_target("support", ID_D, message_ids=["slack:CDUMMY:1"])
        self.assertEqual(self.resolver.skipped_no_session, 1)
        self.resolver.resolve_target("support", ID_D, message_ids=["slack:CDUMMY:1", "slack:CDUMMY:2"])
        self.resolver.resolve_target("content", ID_D, message_ids=["slack:CDUMMY:1"])
        self.assertEqual(self.resolver.skipped_no_session, 3)
        self.resolver.resolve_target("support", ID_D)
        self.resolver.resolve_target("support", ID_D)
        self.assertEqual(self.resolver.skipped_no_session, 4)


class NotLiveSweepTests(TempRoot):
    """Item 2 through the delivery gate: repeated sweeps log once and count pairs once."""

    def test_repeated_sweeps_log_once_and_count_distinct_ids(self):
        sessions = self.root / "sessions"
        sessions.mkdir()
        base = fixture_config(self.root)
        lane_session = replace(base.notify_adapters[-1], kind="session_message", purpose="lane",
                               path=None, dry_run=False, template={},
                               command_template=("dummy-cli", "{target}", "{message}"))
        lanes = tuple(replace(row, session=ID_D) if row.id == "support" else row for row in base.lanes)
        config = replace(base, lanes=lanes, sessions_dir=str(sessions),
                         notify_adapters=(base.notify_adapters[0], lane_session))
        store = self.open_store(config)
        notifiers = build_notifiers(config, store)
        runner = Mock(return_value=SimpleNamespace(returncode=0))
        notifiers[1].runner = runner
        notifiers[1].session_resolver.clock = Clock()
        clock = Clock()
        pipeline = Pipeline(config, store, NullSuggester(), notifiers,
                            WakePolicyEngine(config.wake_policy, clock), clock)
        sweep = TimerSweep(config, store, notifiers, clock, human=pipeline.human)
        stamp = datetime.fromtimestamp(1000, timezone.utc).isoformat()
        with self.assertLogs("slack_dispatch", level="INFO") as logs:
            with patch("slack_dispatch.ledger.store.now", return_value=stamp):
                pipeline.handle_event(event())
                pipeline.handle_event(event(2, user="U0000000089"))
            for minute in range(5):
                sweep.run_once(1000 + config.timers.unacknowledged_seconds + 60 * minute)
        runner.assert_not_called()
        lines = [line for line in logs.output if "session not live" in line]
        self.assertEqual(len(lines), 1)
        self.assertEqual(pipeline.snapshot()["wake_skipped_no_session"], 2)


class WakeSwitchPathTests(TempRoot):
    """Item 3: one helper; wake_policy.state_dir when set, else service.state_dir."""

    def config(self, wake_dir, service_dir):
        base = fixture_config(self.root)
        return replace(base, wake_policy=replace(base.wake_policy, state_dir=wake_dir),
                       service=replace(base.service, state_dir=service_dir))

    def test_precedence(self):
        cases = [("state", "dummy/service-state", Path("dummy/service-state/wake.off")),
                 ("dummy/wake-state", "dummy/service-state", Path("dummy/wake-state/wake.off")),
                 ("state", "state", Path("state/wake.off")),
                 ("dummy/wake-state", "state", Path("dummy/wake-state/wake.off"))]
        for wake_dir, service_dir, expected in cases:
            with self.subTest(wake_dir=wake_dir, service_dir=service_dir):
                self.assertEqual(wake_switch_path(self.config(wake_dir, service_dir)), expected)

    def test_gate_and_builder_read_the_same_switch(self):
        for wake_dir, switch_dir, ignored_dir in (
                ("state", "svc", "wake"), (str(self.root / "wake"), "wake", "svc")):
            with self.subTest(wake_dir=wake_dir):
                config = self.config(wake_dir, str(self.root / "svc"))
                spec = replace(config.notify_adapters[1], kind="session_message",
                               command_template=("dummy", "{target}"))
                config = replace(config, notify_adapters=(config.notify_adapters[0], spec))
                store = self.open_store(config)
                store.upsert_lane("owner", session_pointer="dummy-owner")
                notifier = build_notifiers(config, store)[1]
                ignored = self.root / ignored_dir / "wake.off"
                switch = self.root / switch_dir / "wake.off"
                for path in (ignored, switch):
                    path.parent.mkdir(exist_ok=True)
                    self.addCleanup(path.unlink, missing_ok=True)
                ignored.touch()
                self.assertTrue(can_notify(config, store, spec, "owner", notifier))
                self.assertEqual(notifier.target_resolver("owner"), "dummy-owner")
                switch.touch()
                self.assertFalse(can_notify(config, store, spec, "owner", notifier))
                self.assertIsNone(notifier.target_resolver("owner"))
                switch.unlink()
                ignored.unlink()

    def invoke(self, config):
        # configure_logging turns off propagation on "slack_dispatch"; restore it so later
        # tests that assert on the root logger are not affected by test order.
        logger = logging.getLogger("slack_dispatch")
        self.addCleanup(setattr, logger, "propagate", logger.propagate)
        self.addCleanup(logger.setLevel, logger.level)
        notifiers = [Mock(), Mock(), Mock()]

        def factory(config, on_event, store):
            return [FakeSource(entry, on_event, [event()]) for entry in config.sources if entry.enabled]

        with patch.object(service, "load_config", return_value=config), \
             patch.object(service, "build_sources", side_effect=factory), \
             patch.object(service, "build_notifiers", return_value=notifiers), \
             patch.object(service.time, "time", side_effect=lambda: 1000.0), \
             patch.object(service.time, "sleep", side_effect=lambda _: None):
            self.assertEqual(service.main(["--config", "dummy.yaml", "--once"]), 0)
        return notifiers, (self.root / "logs/service.log").read_text(encoding="utf-8")

    def service_config(self, wake_dir):
        from slack_dispatch.config.loader import load_config
        from tests.test_pipeline_integration import SAMPLE
        base = fixture_config(self.root)
        return replace(base, sources=load_config(SAMPLE).sources,
                       wake_policy=replace(base.wake_policy, state_dir=wake_dir))

    def test_service_warns_once_and_uses_set_wake_policy_dir(self):
        (self.root / "wake").mkdir()
        (self.root / "wake/wake.off").touch()
        notifiers, log = self.invoke(self.service_config(str(self.root / "wake")))
        notifiers[0].notify.assert_called()
        notifiers[1].notify.assert_not_called()
        self.assertEqual(log.count("Wake switch: wake_policy.state_dir and service.state_dir differ"), 1)
        self.assertIn("wake.off is read from wake_policy.state_dir", log)

    def test_service_state_switch_is_ignored_when_wake_policy_dir_is_set(self):
        (self.root / "state").mkdir()
        (self.root / "state/wake.off").touch()
        notifiers, log = self.invoke(self.service_config(str(self.root / "wake")))
        notifiers[1].notify.assert_called()
        self.assertIn("differ", log)

    def test_no_warning_when_dirs_match(self):
        notifiers, log = self.invoke(self.service_config(str(self.root / "state")))
        notifiers[1].notify.assert_called()
        self.assertNotIn("Wake switch", log)


if __name__ == "__main__":
    unittest.main()
