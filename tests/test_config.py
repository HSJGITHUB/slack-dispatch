from pathlib import Path
import unittest

import yaml

from slack_dispatch.config.loader import ConfigError, load_config, parse_config

SAMPLE = Path(__file__).resolve().parents[1] / "examples/instance-sample/config.yaml"


class ConfigTests(unittest.TestCase):
    def test_example_loads(self):
        config = load_config(SAMPLE)
        self.assertEqual(len(config.lanes), 3)
        self.assertIsNone(config.channel_defaults["C0000000002"])
        self.assertEqual(config.timers.unassigned_seconds, 1800)
        self.assertEqual(config.wake_policy.batch_seconds, 600)

    def test_required_sections(self):
        original = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        required = {"lanes", "channel_defaults", "dm_defaults", "person_channel_rules",
                    "explicit_tags", "excluded_channels", "timers", "wake_policy",
                    "notify_adapters", "sources", "ledger"}
        observed = set()
        for key in original:
            with self.subTest(key=key):
                data = dict(original)
                del data[key]
                if key in required:
                    with self.assertRaisesRegex(ConfigError, "config." + key + " is required"):
                        parse_config(data)
                    observed.add(key)
                else:
                    parse_config(data)
        self.assertEqual(observed, required)

    def test_unknown_lane_and_boolean_timer(self):
        data = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        data["explicit_tags"]["[unknown]"] = "missing"
        with self.assertRaisesRegex(ConfigError, "configured lane"):
            parse_config(data)
        data["explicit_tags"] = {}
        data["timers"]["unassigned_seconds"] = True
        with self.assertRaisesRegex(ConfigError, "positive integer"):
            parse_config(data)

    def test_all_optional_sections_omitted_together(self):
        data = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        for key in ("always_human", "classifier", "explicit_tag_pattern", "inbox"):
            data.pop(key)
        for key in ("state_dir", "immediate_users", "owner_user"):
            data["wake_policy"].pop(key, None)
        config = parse_config(data)
        self.assertEqual(config.always_human.users, ())
        self.assertFalse(config.classifier.enabled)
        self.assertEqual(config.explicit_tag_pattern, r"\[(?P<lane>[a-z0-9-]+)\]")
        self.assertEqual(config.inbox.root, "inbox")
        self.assertEqual(config.wake_policy.state_dir, "state")
        self.assertEqual(config.wake_policy.immediate_users, ())
        self.assertIsNone(config.wake_policy.owner_user)
        self.assertEqual(config.ledger.path, data["ledger"]["path"])
        self.assertEqual(config.sources[0].env_file, data["sources"][0]["env_file"])
        self.assertEqual(config.sources[1].lookback_days, 7)

    def test_legacy_adapter_alias_remains_supported(self):
        from slack_dispatch.notify import build_notifiers
        from slack_dispatch.notify.file_drop import FileDropNotifier
        from tests.fakes.fake_store import FakeStore

        data = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        data["notify_adapters"] = [{"kind": "assistant_dm", "path": "dummy-outbox"}]
        adapters = build_notifiers(parse_config(data), FakeStore())
        self.assertIsInstance(adapters[0], FileDropNotifier)
