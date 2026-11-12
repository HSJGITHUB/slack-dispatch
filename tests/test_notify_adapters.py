from dataclasses import replace
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import yaml

from slack_dispatch.config.loader import ConfigError, load_config, parse_config
from slack_dispatch.inbox.files import read
from slack_dispatch.notify import build_notifiers
from slack_dispatch.notify.assistant_dm import AssistantDMNotifier
from slack_dispatch.notify.file_drop import FileDropNotifier
from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.session_message import NotifyError, SessionMessageNotifier
from tests.fakes.fake_store import FakeStore

SAMPLE = Path(__file__).resolve().parents[1] / "examples/instance-sample/config.yaml"


class NotifierTests(unittest.TestCase):
    def test_file_inbox_lookup(self):
        with tempfile.TemporaryDirectory() as root:
            notifier = FileInboxNotifier(root, lambda mid: {"channel": "CDUMMY", "ts": "2", "thread_ts": "1", "followup": True, "body": "PRIVATE_BODY"})
            notifier.notify("support", ["slack:CDUMMY:2"])
            row = read(root, "support")[0]
            self.assertEqual(row["kind"], "followup")
            self.assertEqual(row["thread_ts"], "1")
            self.assertNotIn("PRIVATE_BODY", str(row))
            FileInboxNotifier(root).notify("support", ["slack:CDUMMY:3"])
            self.assertEqual(len(read(root, "support")), 2)

    def test_session_retry_skip_and_dry_run(self):
        runner = Mock(side_effect=[subprocess.TimeoutExpired("dummy", 1), subprocess.CompletedProcess([], 0)])
        notifier = SessionMessageNotifier(["dummy", "{target}", "{message}"], lambda lane: "dummy-target", runner=runner)
        notifier.notify("support", ["slack:CDUMMY:1"])
        self.assertEqual(runner.call_count, 2)
        args, kwargs = runner.call_args
        self.assertEqual(args[0][1], "dummy-target")
        self.assertIn("ids: slack:CDUMMY:1", args[0][2])
        self.assertFalse(kwargs["shell"])
        runner.reset_mock()
        runner.side_effect = OSError("PRIVATE_ERROR")
        with self.assertRaisesRegex(NotifyError, "two attempts") as caught:
            notifier.notify("support", ["slack:CDUMMY:1"])
        self.assertNotIn("PRIVATE_ERROR", str(caught.exception))
        self.assertEqual(runner.call_count, 2)
        runner.reset_mock()
        notifier.target_resolver = lambda lane: None
        with self.assertLogs(level="INFO"):
            notifier.notify("support", ["slack:CDUMMY:1"])
        runner.assert_not_called()
        notifier.target_resolver = lambda lane: "PRIVATE_TARGET"
        notifier.dry_run = True
        with self.assertLogs(level="INFO") as logs:
            notifier.notify("support", ["slack:CDUMMY:1"])
        self.assertNotIn("PRIVATE_TARGET", str(logs.output))
        runner.assert_not_called()

    def test_atomic_file_drop(self):
        self.assertIs(AssistantDMNotifier, FileDropNotifier)
        with tempfile.TemporaryDirectory() as root:
            notifier = FileDropNotifier(root, {"meta": {"ids": "{ids}", "count": "{count}"}, "title": "{title}", "body": "{body}", "lane": "{lane}"})
            notifier.notify("support", ["slack:CDUMMY:1"])
            notifier.notify("support", ["slack:CDUMMY:1"])
            files = list(Path(root).glob("*.json"))
            self.assertEqual(len(files), 2)
            self.assertEqual(list(Path(root).glob("*.tmp")), [])
            payload = json.loads(files[0].read_text())
            self.assertEqual(payload["meta"], {"ids": ["slack:CDUMMY:1"], "count": 1})
            self.assertIn("Run: inbox support", payload["body"])

    def test_build_and_config(self):
        config = load_config(SAMPLE)
        store = FakeStore()
        adapters = build_notifiers(config, store)
        self.assertEqual([type(a) for a in adapters], [FileInboxNotifier, SessionMessageNotifier, FileDropNotifier])
        self.assertEqual(adapters[1].target_resolver("support"), "session-support-placeholder")
        self.assertIsNone(adapters[1].target_resolver("content"))
        store.upsert_lane("support", session_pointer="dummy-current")
        self.assertEqual(adapters[1].target_resolver("support"), "dummy-current")
        store.upsert_lane("support", paused=True)
        self.assertIsNone(adapters[1].target_resolver("support"))
        for field, value in (("immediate_users", "wrong"), ("owner_user", 42)):
            raw = yaml.safe_load(SAMPLE.read_text())
            raw["wake_policy"][field] = value
            with self.assertRaises(ConfigError):
                parse_config(raw)

    def test_disabled_config_preserves_file_adapter(self):
        config = load_config(SAMPLE)
        config = replace(config, wake_policy=replace(config.wake_policy, enabled=False))
        adapters = build_notifiers(config, FakeStore())
        self.assertIsNone(adapters[1].target_resolver("support"))
        self.assertIsInstance(adapters[0], FileInboxNotifier)

    def test_omitted_inbox_and_state_dir_defaults(self):
        raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        raw.pop("inbox")
        raw["wake_policy"].pop("state_dir", None)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.yaml"
            path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            config = load_config(path)
        self.assertEqual(config.inbox.root, "inbox")
        self.assertEqual(config.wake_policy.state_dir, "state")
        for invalid in (None, "", 42):
            raw["wake_policy"]["state_dir"] = invalid
            with self.assertRaises(ConfigError):
                parse_config(raw)

    def test_exact_session_placeholders_preserve_other_braces(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0))
        notifier = SessionMessageNotifier(
            ["dummy", '{"unknown": true}', "{target}", "{message}", "{open"],
            lambda lane: "dummy-{message}", runner=runner)
        notifier.notify("support", ["slack:CDUMMY:1"])
        command = runner.call_args.args[0]
        self.assertEqual(command[1], '{"unknown": true}')
        self.assertEqual(command[2], "dummy-{message}")
        self.assertIn("Run: inbox support", command[3])
        self.assertEqual(command[4], "{open")
        notifier.dry_run = True
        notifier.command_template = ["dummy", '{"target": "{target}", "note": "{unknown}"}']
        with self.assertLogs(level="INFO") as logs:
            notifier.notify("support", ["slack:CDUMMY:1"])
        self.assertIn("{unknown}", str(logs.output))
        self.assertNotIn("dummy-{message}", str(logs.output))

    def test_exact_file_drop_placeholders_preserve_other_braces(self):
        with tempfile.TemporaryDirectory() as root:
            template = {"text": '{"lane": "{lane}", "other": {unknown}}',
                        "values": ["{ids}", "{count}", "{title}", "{body}",
                                   "ids={ids}; count={count}; {open"]}
            FileDropNotifier(root, template).notify("support", ["dummy-{lane}"])
            payload = json.loads(next(Path(root).glob("*.json")).read_text())
            self.assertEqual(payload["text"], '{"lane": "support", "other": {unknown}}')
            self.assertEqual(payload["values"][:3], [["dummy-{lane}"], 1, "Lane support"])
            self.assertIn("dummy-{lane}", payload["values"][3])
            self.assertEqual(payload["values"][4], "ids=dummy-{lane}; count=1; {open")

    def test_configured_state_dir_is_shared(self):
        from slack_dispatch.notify.wake import Defer, Deliver, WakePolicyEngine

        with tempfile.TemporaryDirectory() as root:
            raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
            raw["wake_policy"]["state_dir"] = str(Path(root) / "custom-state")
            config = parse_config(raw)
            engine = WakePolicyEngine(config.wake_policy, lambda: 0)
            adapters = build_notifiers(config, FakeStore())
            self.assertEqual(adapters[1].target_resolver("support"), "session-support-placeholder")
            self.assertIsInstance(engine.offer("support", {"id": "m1"}, "dummy"), Deliver)
            switch = Path(config.wake_policy.state_dir) / "wake.off"
            switch.parent.mkdir()
            switch.touch()
            self.assertIsNone(adapters[1].target_resolver("support"))
            decision = engine.offer("support", {"id": "m2"}, "dummy")
            self.assertIsInstance(decision, Defer)
            self.assertEqual(decision.reason, "disabled")
            self.assertEqual(engine.flush(100), [])
            switch.rename(switch.with_name("wake.disabled"))
            self.assertEqual(adapters[1].target_resolver("support"), "session-support-placeholder")
            self.assertTrue(engine.enabled())
