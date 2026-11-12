from copy import deepcopy
from pathlib import Path
import unittest

import yaml

from slack_dispatch.config.loader import ConfigError, parse_config
from slack_dispatch.router.rules import DEFAULT_TAG_PATTERN

SAMPLE = Path(__file__).resolve().parents[1] / "examples/instance-sample/config.yaml"


def raw_config():
    return yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))


class RouterConfigTests(unittest.TestCase):
    def test_optional_defaults(self):
        raw = raw_config()
        for key in ("always_human", "classifier", "explicit_tag_pattern"):
            del raw[key]
        config = parse_config(raw)
        self.assertEqual(config.always_human.users, ())
        self.assertEqual(config.always_human.mention_users, ())
        self.assertIsNone(config.always_human.owner_user)
        self.assertEqual(config.classifier.kind, "none")
        self.assertFalse(config.classifier.enabled)
        self.assertEqual(config.explicit_tag_pattern, DEFAULT_TAG_PATTERN)

    def test_added_values(self):
        raw = raw_config()
        raw["classifier"].update(kind="anthropic", enabled=True)
        raw["explicit_tag_pattern"] = r"#(?P<lane>[a-z]+)"
        config = parse_config(raw)
        self.assertEqual(config.always_human.owner_user, "U0000000093")
        self.assertTrue(config.classifier.enabled)
        self.assertEqual(config.classifier.lane_descriptions["support"], "Support requests")
        self.assertEqual(config.explicit_tag_pattern, raw["explicit_tag_pattern"])

    def test_invalid_added_values(self):
        cases = [
            ("always_human", None), ("always_human", {"users": "wrong"}),
            ("always_human", {"mention_users": [1]}),
            ("always_human", {"owner_user": 3}),
            ("classifier", {"kind": "unknown"}), ("classifier", {"enabled": "false"}),
            ("classifier", {"enabled": True}),
            ("classifier", {"kind": "anthropic", "enabled": True}),
            ("classifier", {"lane_descriptions": {"missing": "description"}}),
            ("classifier", {"api_key_env": ""}),
            ("explicit_tag_pattern", "["), ("explicit_tag_pattern", "[a-z]+"),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                raw = deepcopy(raw_config())
                raw[key] = value
                with self.assertRaises(ConfigError):
                    parse_config(raw)
