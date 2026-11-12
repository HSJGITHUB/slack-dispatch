from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import Mock

from slack_dispatch.config.loader import parse_config
from slack_dispatch.router.engine import route
from slack_dispatch.router.rules import (
    ChannelDefault, ExplicitTag, PersonChannel, ThreadInherit, DEFAULT_TAG_PATTERN,
)
from tests.fakes.fake_store import message
from tests.test_router_config import raw_config


class EngineTests(unittest.TestCase):
    def test_owner_mention_with_label(self):
        for label in ("dummy-label", ""):
            row = message(text=f"[support] <@U0000000093|{label}>", thread_ts="1")
            self.assertEqual(route(row, self.config, self.lookup).attention, "owner_mention")
        self.assertEqual(self.lookup.call_count, 2)
        row = message(text="<@U00000000930|dummy-label>")
        self.assertEqual(route(row, self.config, self.lookup).rule, "channel_default")

    def setUp(self):
        self.config = parse_config(raw_config())
        self.lookup = Mock(return_value=None)

    def test_each_rule_and_order(self):
        row = message(thread_ts="1", channel="C0000000002")
        self.lookup.return_value = "owner"
        before = deepcopy(row)
        self.assertEqual(route(row, self.config, self.lookup).rule, "thread_inherit")
        self.lookup.return_value = None
        self.assertEqual(route(row, self.config, self.lookup).rule, "explicit_tag")
        row["text"] = None
        self.assertEqual(route(row, self.config, self.lookup).rule, "person_channel")
        row["channel"] = "C0000000001"
        config = replace(self.config, person_channel_rules=(replace(
            self.config.person_channel_rules[0], channel=row["channel"]),))
        self.assertEqual(route(row, config, self.lookup).rule, "channel_default")
        row["channel"] = "C0000000002"
        row["user"] = "U0000000088"
        self.assertEqual(route(row, self.config, self.lookup).rule, "none")
        untouched = deepcopy(before)
        route(untouched, self.config, self.lookup)
        self.assertEqual(untouched, before)

    def test_rules_alone(self):
        self.assertIsNone(ThreadInherit(self.lookup).match(message(thread_ts="2")))
        self.lookup.assert_not_called()
        self.lookup.return_value = "owner"
        self.assertEqual(ThreadInherit(self.lookup).match(message(thread_ts="1")).lane, "owner")
        defaults = ChannelDefault(self.config.channel_defaults, self.config.dm_defaults)
        self.assertIsNone(defaults.match(message(channel="C0000000002")))
        for kind in ("im", "mpim", None):
            self.assertEqual(defaults.match(message(channel="D0000000001", channel_type=kind)).lane,
                             "owner")
        self.assertIsNone(defaults.match(message(channel_type="im")))
        person = PersonChannel(self.config.person_channel_rules)
        self.assertEqual(person.match(message(channel="C0000000002")).lane, "content")
        self.assertIsNone(person.match(message()))
        row = message(channel="C0000000002")
        row["author"] = row.pop("user")
        self.assertEqual(person.match(row).lane, "content")

    def test_tags(self):
        rule = ExplicitTag(self.config.explicit_tags, DEFAULT_TAG_PATTERN)
        for text in (None, "support", "[unknown]", "[SUPPORT]", "[support extra]"):
            self.assertIsNone(rule.match(message(text=text)))
        self.assertEqual(rule.match(message(text="[unknown] [content] [support]")).lane, "content")
        custom = ExplicitTag({"support": "support"}, r"#(?P<lane>[a-z]+)")
        self.assertEqual(custom.match(message(text="#support")).lane, "support")

    def test_protection_precedence(self):
        self.lookup.return_value = "owner"
        for changes in ({"user": "U0000000091"}, {"text": "[support] <@U0000000092>"},
                        {"text": "<@U0000000093>"}):
            row = message(thread_ts="1", **changes)
            self.assertEqual(route(row, self.config, self.lookup).rule, "thread_inherit")
            self.assertIsNotNone(route(row, self.config, self.lookup).attention)
            row["channel"] = "C0000000003"
            self.assertEqual(route(row, self.config, self.lookup).rule, "excluded")
        self.assertEqual(route(message(text=None, user="U0000000091"), self.config,
                               self.lookup).attention, "always_human")
        for text in (None, "U0000000093", "<@U00000000930>"):
            self.assertEqual(route(message(text=text), self.config, self.lookup).rule,
                             "channel_default")
