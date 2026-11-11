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
        for key in original:
            with self.subTest(key=key):
                data = dict(original)
                del data[key]
                with self.assertRaisesRegex(ConfigError, "config." + key + " is required"):
                    parse_config(data)

    def test_unknown_lane_and_boolean_timer(self):
        data = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        data["explicit_tags"]["[unknown]"] = "missing"
        with self.assertRaisesRegex(ConfigError, "configured lane"):
            parse_config(data)
        data["explicit_tags"] = {}
        data["timers"]["unassigned_seconds"] = True
        with self.assertRaisesRegex(ConfigError, "positive integer"):
            parse_config(data)
