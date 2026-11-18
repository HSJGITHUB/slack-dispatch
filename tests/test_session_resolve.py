import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from slack_dispatch import service
from slack_dispatch.config.loader import ConfigError, load_config, parse_config
from slack_dispatch.inbox.cli import main
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify import build_notifiers, effective_lane
from slack_dispatch.notify.session_resolve import SessionResolver, is_session_id
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline
from slack_dispatch.router.classifier import NullSuggester
from slack_dispatch.runtime import read_heartbeat
from slack_dispatch.timers import TimerSweep
from tests.fakes.fake_store import FakeStore
from tests.test_pipeline_integration import event, fixture_config
from tests.test_service_integration import FakeSource

SAMPLE = Path(__file__).resolve().parents[1] / "examples/instance-sample/config.yaml"
ID_A = "0000aaaa-0000-4000-8000-000000000001"
ID_B = "0000aaaa-0000-4000-8000-000000000002"
ID_C = "1111bbbb-0000-4000-8000-000000000003"
ID_D = "2222cccc-0000-4000-8000-000000000004"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class SessionDirTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name) / "sessions"
        self.dir.mkdir()
        self.clock = Clock()

    def write(self, file_name, session_id, name):
        payload = {"sessionId": session_id, "name": name, "bridgeSessionId": "dummy-bridge",
                   "messagingSocketPath": "dummy-socket"}
        (self.dir / file_name).write_text(json.dumps(payload), encoding="utf-8")

    def resolver(self):
        return SessionResolver(str(self.dir), clock=self.clock)


class ResolveTargetTests(SessionDirTest):
    def setUp(self):
        super().setUp()
        self.write("1.json", ID_A, "dummy-alpha")
        self.write("2.json", ID_B, "dummy-beta")
        self.write("3.json", ID_C, "dummy-gamma")

    def test_value_forms(self):
        for value in (ID_A, ID_A.upper(), "0000aaaa", "0000aaaa-0000"):
            self.assertTrue(is_session_id(value), value)
        for value in ("dummy-alpha", "0000aaa", "sess-f4", "0000aaaz", "", None):
            self.assertFalse(is_session_id(value), value)

    def test_exact_uuid_and_prefix(self):
        resolver = self.resolver()
        self.assertEqual(resolver.resolve_target("support", ID_A), "dummy-alpha")
        self.assertEqual(resolver.resolve_target("support", ID_B.upper()), "dummy-beta")
        self.assertEqual(resolver.resolve_target("support", "1111bbbb"), "dummy-gamma")
        self.assertEqual(resolver.resolve_target("support", "1111BBBB-0000"), "dummy-gamma")

    def test_ambiguous_prefix(self):
        resolver = self.resolver()
        with self.assertLogs("slack_dispatch.notify.session_resolve", level="INFO") as logs:
            self.assertIsNone(resolver.resolve_target("support", "0000aaaa"))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("ambiguous session id", logs.output[0])
        self.assertIn("support", logs.output[0])
        self.assertNotIn("0000aaaa", logs.output[0])
        self.assertEqual(resolver.skipped_no_session, 0)

    def test_not_live(self):
        resolver = self.resolver()
        with self.assertLogs("slack_dispatch.notify.session_resolve", level="INFO") as logs:
            self.assertIsNone(resolver.resolve_target("support", ID_D))
            self.assertIsNone(resolver.resolve_target("content", "2222cccc"))
        self.assertEqual(len(logs.output), 2)
        self.assertTrue(logs.output[0].startswith("INFO:"))
        self.assertIn("session not live: lane support", logs.output[0])
        self.assertIn("session not live: lane content", logs.output[1])
        self.assertNotIn("2222cccc", str(logs.output))
        self.assertEqual(resolver.skipped_no_session, 2)

    def test_plain_name_passthrough(self):
        resolver = self.resolver()
        with self.assertNoLogs("slack_dispatch.notify.session_resolve", level="INFO"):
            self.assertEqual(resolver.resolve_target("support", "dummy-alpha"), "dummy-alpha")
            self.assertEqual(resolver.resolve_target("support", "dummy-not-live"), "dummy-not-live")
        missing = SessionResolver(str(self.dir / "absent"), clock=self.clock)
        self.assertEqual(missing.resolve_target("support", "dummy-name"), "dummy-name")
        self.assertIsNone(missing.resolve_target("support", ID_A))
        self.assertIsNone(missing.resolve_target("support", None))

    def test_duplicate_names(self):
        self.write("4.json", ID_D, "dummy-alpha")
        resolver = self.resolver()
        with self.assertLogs("slack_dispatch.notify.session_resolve", level="INFO") as logs:
            self.assertEqual(resolver.resolve_target("support", "dummy-alpha"), "dummy-alpha")
        self.assertIn("duplicate session name", logs.output[0])
        self.assertIn("lane support, 2 live sessions", logs.output[0])
        self.assertIn("addressed by name", logs.output[0])
        self.assertNotIn("dummy-alpha", str(logs.output))
        # An id lane picks the file whose sessionId matches, not the other duplicate.
        with self.assertLogs("slack_dispatch.notify.session_resolve", level="INFO") as logs:
            self.assertEqual(resolver.resolve_target("support", "2222cccc"), "dummy-alpha")
        self.assertIn("addressed by id", logs.output[0])
        with self.assertNoLogs("slack_dispatch.notify.session_resolve", level="INFO"):
            self.assertEqual(resolver.resolve_target("support", ID_B), "dummy-beta")

    def test_cache_expiry_with_fake_clock(self):
        resolver = self.resolver()
        self.assertIsNone(resolver.resolve_target("support", ID_D))
        self.write("4.json", ID_D, "dummy-delta")
        self.clock.now += 29
        self.assertIsNone(resolver.resolve_target("support", ID_D))
        self.clock.now += 1
        self.assertEqual(resolver.resolve_target("support", ID_D), "dummy-delta")
        (self.dir / "4.json").rename(self.dir / "4.json.moved")
        self.clock.now += 10
        self.assertEqual(resolver.resolve_target("support", ID_D), "dummy-delta")
        self.clock.now -= 100
        self.assertIsNone(resolver.resolve_target("support", ID_D))

    def test_unreadable_files_are_ignored(self):
        (self.dir / "bad.json").write_text("{not json", encoding="utf-8")
        (self.dir / "list.json").write_text("[]", encoding="utf-8")
        (self.dir / "noname.json").write_text(json.dumps({"sessionId": ID_D}), encoding="utf-8")
        (self.dir / "other.txt").write_text(json.dumps({"sessionId": ID_D, "name": "x"}),
                                            encoding="utf-8")
        resolver = self.resolver()
        self.assertIsNone(resolver.resolve_target("support", ID_D))
        self.assertEqual(resolver.resolve_target("support", ID_A), "dummy-alpha")


class BuilderPrecedenceTests(SessionDirTest):
    def config(self, **lanes):
        config = load_config(SAMPLE)
        rows = tuple(replace(row, **lanes[row.id]) if row.id in lanes else row
                     for row in config.lanes)
        return replace(config, lanes=rows, sessions_dir=str(self.dir))

    def test_config_session_wins_and_ledger_applies_when_null(self):
        self.write("1.json", ID_A, "dummy-alpha")
        store = FakeStore()
        config = self.config(support={"session": "0000aaaa"})
        resolve = build_notifiers(config, store)[1].target_resolver
        store.upsert_lane("support", session_pointer="dummy-stale")
        self.assertEqual(resolve("support"), "dummy-alpha")
        self.assertEqual(effective_lane(config, store, "support"), ("0000aaaa", False))
        self.assertIsNone(resolve("owner"))
        store.upsert_lane("owner", session_pointer=ID_A)
        self.assertEqual(resolve("owner"), "dummy-alpha")
        store.upsert_lane("owner", session_pointer="dummy-claimed")
        self.assertEqual(resolve("owner"), "dummy-claimed")
        store.upsert_lane("human_queue", session_pointer="dummy-human")
        self.assertEqual(build_notifiers(config, store)[1].target_resolver("human_queue"),
                         "dummy-human")

    def test_config_pause_always_wins(self):
        store = FakeStore()
        config = self.config(support={"paused": True})
        resolve = build_notifiers(config, store)[1].target_resolver
        store.upsert_lane("support", paused=False)
        self.assertIsNone(resolve("support"))
        self.assertEqual(effective_lane(config, store, "support")[1], True)
        store.upsert_lane("owner", session_pointer="dummy-claimed", paused=True)
        self.assertIsNone(resolve("owner"))
        store.upsert_lane("owner", paused=False)
        self.assertEqual(resolve("owner"), "dummy-claimed")

    def test_not_live_id_skips_session_command(self):
        store = FakeStore()
        config = self.config(support={"session": ID_D})
        notifier = build_notifiers(config, store)[1]
        notifier.dry_run = False
        notifier.runner = Mock()
        # Named logger: service tests turn off propagation on "slack_dispatch".
        with self.assertLogs("slack_dispatch", level="INFO") as logs:
            notifier.notify("support", ["slack:CDUMMY:1"])
        notifier.runner.assert_not_called()
        self.assertIn("session not live: lane support", str(logs.output))
        self.assertIn("no live session", str(logs.output))
        self.assertEqual(notifier.session_resolver.skipped_no_session, 1)


class DeliveryGateTests(SessionDirTest):
    """The pipeline and timer gate share the builder precedence and id resolution."""

    UNACKNOWLEDGED = 1000 + 14400  # sample timers.unacknowledged_seconds after assignment

    def setUp(self):
        super().setUp()
        self.root = self.dir.parent

    def config(self, **support):
        base = fixture_config(self.root)
        session = replace(load_config(SAMPLE).notify_adapters[1], purpose="lane", dry_run=False)
        lanes = tuple(replace(row, **support) if row.id == "support" else row for row in base.lanes)
        return replace(base, lanes=lanes, sessions_dir=str(self.dir),
                       notify_adapters=(base.notify_adapters[0], session))

    def build(self, **support):
        self.config = self.config(**support)
        self.store = LedgerStore(self.config.ledger.path)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id)  # ledger session_pointer stays NULL
        self.notifiers = build_notifiers(self.config, self.store)
        self.runner = Mock(return_value=SimpleNamespace(returncode=0))
        self.notifiers[1].runner = self.runner
        self.notifiers[1].session_resolver.clock = self.clock
        clock = lambda: 1000.0  # noqa: E731
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(), self.notifiers,
                                 WakePolicyEngine(self.config.wake_policy, clock), clock)
        self.sweep = TimerSweep(self.config, self.store, self.notifiers, clock,
                                human=self.pipeline.human)

    def assign(self):
        stamp = datetime.fromtimestamp(1000, timezone.utc).isoformat()
        with patch("slack_dispatch.ledger.store.now", return_value=stamp):
            result = self.pipeline.handle_event(event())
        self.assertEqual((result.state, result.lane), ("assigned", "support"))
        return result.message_id

    def targets(self):
        return [call.args[0][call.args[0].index("--target") + 1]
                for call in self.runner.call_args_list]

    def receipts(self, mid):
        return [row["timer"] for row in
                self.store._rows("SELECT timer FROM timer_events WHERE message_id=?", (mid,))]

    def test_config_session_wakes_with_null_ledger_pointer(self):
        self.write("1.json", ID_A, "dummy-alpha")
        self.build(session="0000aaaa")
        self.assertIsNone(self.store.get_lane("support")["session_pointer"])
        mid = self.assign()
        self.assertEqual(self.targets(), ["dummy-alpha"])
        self.assertEqual(self.sweep.run_once(self.UNACKNOWLEDGED), 1)
        self.assertEqual(self.targets(), ["dummy-alpha", "dummy-alpha"])
        self.assertEqual(self.receipts(mid), ["unacknowledged"])

    def test_config_pause_blocks_wake_despite_ledger(self):
        self.build(session="dummy-support", paused=True)
        self.store.upsert_lane("support", session_pointer="dummy-support", paused=False)
        self.assertEqual(self.store.get_lane("support")["paused"], 0)
        mid = self.assign()
        self.assertEqual(self.sweep.run_once(self.UNACKNOWLEDGED), 0)
        self.runner.assert_not_called()
        self.assertEqual(self.receipts(mid), [])
        self.assertTrue((self.root / "inbox/support.jsonl").exists())

    def test_not_live_id_is_undelivered_and_timer_retries(self):
        self.build(session=ID_D)
        mid = self.assign()
        self.runner.assert_not_called()
        # Only the durable inbox counts; the skipped wake is not a delivery.
        self.assertEqual(self.pipeline.snapshot()["delivered"], 1)
        self.assertEqual(self.sweep.run_once(self.UNACKNOWLEDGED), 0)
        self.assertEqual(self.receipts(mid), [])
        self.runner.assert_not_called()
        self.write("4.json", ID_D, "dummy-delta")
        self.clock.now += 30
        self.assertEqual(self.sweep.run_once(self.UNACKNOWLEDGED + 60), 1)
        self.assertEqual(self.targets(), ["dummy-delta"])
        self.assertEqual(self.receipts(mid), ["unacknowledged"])
        self.assertEqual(self.sweep.run_once(self.UNACKNOWLEDGED + 120), 0)
        self.assertEqual(self.runner.call_count, 1)
        # P9: distinct (lane, message id) pairs, not attempts (pipeline and timer retry share one).
        self.assertEqual(self.pipeline.snapshot()["wake_skipped_no_session"], 1)

    def test_ambiguous_id_is_undelivered(self):
        self.write("1.json", ID_A, "dummy-alpha")
        self.write("2.json", ID_B, "dummy-beta")
        self.build(session="0000aaaa")
        mid = self.assign()
        self.assertEqual(self.sweep.run_once(self.UNACKNOWLEDGED), 0)
        self.runner.assert_not_called()
        self.assertEqual(self.receipts(mid), [])
        self.assertEqual(self.pipeline.snapshot()["delivered"], 1)

    def test_heartbeat_reports_wake_skipped_no_session(self):
        config = replace(self.config(session=ID_D), sources=load_config(SAMPLE).sources)

        def factory(config, on_event, store):
            return [FakeSource(entry, on_event, [event()])
                    for entry in config.sources if entry.enabled]

        with patch.object(service, "load_config", return_value=config), \
             patch.object(service, "build_sources", side_effect=factory):
            self.assertEqual(service.main(["--config", "dummy.yaml", "--once"]), 0)
        heartbeat = read_heartbeat(self.root / "state/heartbeat.json")
        self.assertEqual(heartbeat["counters"]["wake_skipped_no_session"], 1)
        self.assertEqual(heartbeat["counters"]["delivered"], 1)
        self.assertTrue(heartbeat["ok"])


class LoaderTests(unittest.TestCase):
    def test_sessions_dir(self):
        raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(parse_config(raw).sessions_dir, "dummy/sessions")
        self.assertFalse(Path(parse_config(raw).sessions_dir).is_absolute())
        raw["sessions_dir"] = "dummy/other-sessions"
        self.assertEqual(parse_config(raw).sessions_dir, "dummy/other-sessions")
        raw.pop("sessions_dir")
        self.assertEqual(parse_config(raw).sessions_dir, "~/.claude/sessions")
        for invalid in (None, "", 42, ["dummy"]):
            raw["sessions_dir"] = invalid
            with self.assertRaises(ConfigError):
                parse_config(raw)


class ClaimTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = FakeStore()
        raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    def run_cli(self, args, env=None):
        out, err = io.StringIO(), io.StringIO()
        environ = {"SLACK_DISPATCH_CONFIG": "", **(env or {})}
        with patch.dict("os.environ", environ):
            code = main(["--inbox-root", str(self.root / "inbox")] + args,
                        store=self.store, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_claim_accepts_all_forms_verbatim(self):
        for value in ("dummy-name", ID_A, "0000aaaa", "0000AAAA-00"):
            with self.subTest(value=value):
                code, out, err = self.run_cli(["claim", "owner", "--session", value])
                self.assertEqual(code, 0)
                self.assertEqual(out, "owner claimed\n")
                self.assertEqual(err, "")
                self.assertEqual(self.store.get_lane("owner")["session_pointer"], value)

    def test_claim_warns_when_config_session_wins(self):
        for args, env in ((["--config", str(self.config_path)], None),
                          ([], {"SLACK_DISPATCH_CONFIG": str(self.config_path)})):
            with self.subTest(args=args):
                code, out, err = self.run_cli(["claim", "support", "--session", ID_A] + args, env)
                self.assertEqual(code, 0)
                self.assertEqual(out, "support claimed\n")
                self.assertIn("warning: lane support has a configured session; config wins", err)
                self.assertEqual(self.store.get_lane("support")["session_pointer"], ID_A)
                code, _, err = self.run_cli(["claim", "owner", "--session", ID_A] + args, env)
                self.assertEqual((code, err), (0, ""))

    def test_claim_with_unreadable_config_does_not_write(self):
        code, _, err = self.run_cli(["claim", "owner", "--session", ID_A,
                                     "--config", str(self.root / "missing.yaml")])
        self.assertEqual((code, err), (2, "Invalid input\n"))
        self.assertIsNone(self.store.get_lane("owner"))


if __name__ == "__main__":
    unittest.main()
