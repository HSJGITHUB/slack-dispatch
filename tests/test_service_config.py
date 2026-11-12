"""Service configuration additions retain the existing required sections."""
import copy
from pathlib import Path
import unittest

import yaml

from slack_dispatch.config.loader import ConfigError, parse_config
from slack_dispatch.config.schema import NotifyAdapter, Service

SAMPLE = Path(__file__).resolve().parents[1] / "examples/instance-sample/config.yaml"


class ServiceConfigTests(unittest.TestCase):
    def setUp(self):
        self.raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))

    def test_optional_service_defaults_and_existing_adapter_default(self):
        del self.raw["service"]
        config = parse_config(self.raw)
        self.assertEqual(config.service, Service())
        self.assertTrue(all(adapter.purpose == "lane" for adapter in config.notify_adapters))
        self.assertEqual(NotifyAdapter("file_drop", "dummy").purpose, "lane")

    def test_kind_agnostic_purpose_and_alias_with_explicit_precedence(self):
        for kind in ("file_inbox", "file_drop", "assistant_dm", "session_message"):
            for key in ("purpose", "for"):
                with self.subTest(kind=kind, key=key):
                    self.raw["notify_adapters"] = [{"kind": kind, "path": "dummy", key: "human_queue"}]
                    config = parse_config(self.raw)
                    self.assertEqual(config.notify_adapters[0].purpose, "human_queue")
        self.raw["notify_adapters"][0].update(purpose="lane", **{"for": "human_queue"})
        self.assertEqual(parse_config(self.raw).notify_adapters[0].purpose, "lane")

    def test_invalid_service_and_purpose_values(self):
        invalid = {"state_dir": [None, ""], "log_dir": [None, ""],
                   "heartbeat_seconds": [0, -1, True, "30"],
                   "followup_window_seconds": [0, -1, False, 1.5],
                   "once": [1, "false"], "log_level": [None, "VERBOSE"]}
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    raw = copy.deepcopy(self.raw)
                    raw["service"][key] = value
                    with self.assertRaises(ConfigError):
                        parse_config(raw)
        for value in (None, [], "queue", True):
            self.raw["notify_adapters"][0]["purpose"] = value
            with self.assertRaises(ConfigError):
                parse_config(self.raw)

    def test_partial_service_map(self):
        self.raw["service"] = {"once": True, "log_level": "debug", "followup_window_seconds": 15}
        service = parse_config(self.raw).service
        self.assertTrue(service.once)
        self.assertEqual(service.log_level, "DEBUG")
        self.assertEqual(service.followup_window_seconds, 15)
        self.assertEqual(service.state_dir, "state")
