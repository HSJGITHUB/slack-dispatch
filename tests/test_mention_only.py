"""P26 mention-only channels: config flag, routing and the pipeline exclusion.

Offline only: the real SQLite ledger in a temporary directory, mock notifiers and a fixed
clock. Dummy ids only.
"""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from slack_dispatch.config.loader import ConfigError, parse_config
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline
from slack_dispatch.router.classifier import NullSuggester
from slack_dispatch.router.engine import NO_MENTION, route
from tests.fakes.fake_store import message
from tests.test_pipeline_integration import event, fixture_config
from tests.test_router_config import raw_config

MENTION_ONLY = "C0000000004"
DEFAULT_LANE = "owner"
OWNER = "U0000000093"
MENTION_USER = "U0000000092"
PROTECTED = "U0000000091"
PLAIN = "U0000000088"


class ConfigTests(unittest.TestCase):
    def test_sample_flags_one_channel_and_keeps_the_defaults_shape(self):
        config = parse_config(raw_config())
        self.assertEqual(config.mention_only_channels, (MENTION_ONLY,))
        self.assertEqual(config.channel_defaults[MENTION_ONLY], DEFAULT_LANE)
        self.assertEqual(config.channel_defaults["C0000000001"], "support")
        self.assertIsNone(config.channel_defaults["C0000000002"])

    def test_flag_is_optional_and_false_by_default(self):
        raw = raw_config()
        raw["channel_defaults"][MENTION_ONLY] = {"default": DEFAULT_LANE}
        self.assertEqual(parse_config(raw).mention_only_channels, ())
        raw["channel_defaults"][MENTION_ONLY] = {"default": DEFAULT_LANE, "mention_only": False}
        self.assertEqual(parse_config(raw).mention_only_channels, ())
        raw["channel_defaults"]["C0000000002"] = {"default": None, "mention_only": True}
        self.assertEqual(parse_config(raw).mention_only_channels, ("C0000000002",))

    def test_loader_rejects_a_non_boolean_flag(self):
        for value in ("true", "yes", 1, 0, None, [True]):
            with self.subTest(value=value):
                raw = deepcopy(raw_config())
                raw["channel_defaults"][MENTION_ONLY] = {"default": DEFAULT_LANE,
                                                         "mention_only": value}
                with self.assertRaises(ConfigError) as caught:
                    parse_config(raw)
                self.assertIn("mention_only", str(caught.exception))

    def test_loader_rejects_the_flag_in_dm_defaults(self):
        raw = raw_config()
        raw["dm_defaults"]["D0000000001"] = {"default": "owner", "mention_only": True}
        with self.assertRaises(ConfigError):
            parse_config(raw)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.config = parse_config(raw_config())
        self.lookup = Mock(return_value=None)

    def route(self, **changes):
        row = message(**dict({"channel": MENTION_ONLY, "user": PLAIN, "text": "dummy text",
                              "thread_ts": None}, **changes))
        return route(row, self.config, self.lookup)

    def test_root_without_mention_is_excluded_with_reason(self):
        decision = self.route()
        self.assertEqual((decision.lane, decision.reason, decision.rule, decision.attention),
                         (None, NO_MENTION, "excluded", None))
        self.lookup.assert_not_called()

    def test_explicit_tag_does_not_bypass_the_flag(self):
        self.assertEqual(self.route(text="[support] dummy").reason, NO_MENTION)

    def test_attention_continues_through_the_normal_precedence(self):
        cases = (({"text": f"<@{OWNER}> dummy"}, "owner_mention"),
                 ({"text": f"<@{MENTION_USER}|dummy-label> dummy"}, "owner_mention"),
                 ({"user": PROTECTED}, "always_human"))
        for changes, attention in cases:
            with self.subTest(changes=changes):
                decision = self.route(**changes)
                self.assertEqual((decision.lane, decision.rule, decision.attention),
                                 (DEFAULT_LANE, "channel_default", attention))
        tagged = self.route(text=f"[support] <@{OWNER}>")
        self.assertEqual((tagged.lane, tagged.rule, tagged.attention),
                         ("support", "explicit_tag", "owner_mention"))

    def test_reply_in_bound_thread_inherits_and_unbound_reply_is_excluded(self):
        self.lookup.return_value = "support"
        decision = self.route(ts="2000.000002", thread_ts="2000.000001")
        self.assertEqual((decision.lane, decision.reason, decision.rule, decision.attention),
                         ("support", "thread_inherit", "thread_inherit", None))
        self.lookup.assert_called_once_with("slack", MENTION_ONLY, "2000.000001")
        self.lookup.return_value = None
        self.assertEqual(self.route(ts="2000.000002", thread_ts="2000.000001").reason, NO_MENTION)

    def test_null_default_with_mention_goes_to_the_human_queue(self):
        config = replace(self.config, channel_defaults=dict(self.config.channel_defaults,
                                                            **{MENTION_ONLY: None}))
        row = message(channel=MENTION_ONLY, user=PLAIN, text=f"<@{OWNER}>", thread_ts=None)
        decision = route(row, config, self.lookup)
        self.assertEqual((decision.lane, decision.rule, decision.attention),
                         (None, "always_human", "owner_mention"))

    def test_excluded_channel_and_bot_policy_come_first(self):
        config = replace(self.config, excluded_channels=(MENTION_ONLY,))
        row = message(channel=MENTION_ONLY, text=f"<@{OWNER}>", thread_ts=None)
        self.assertEqual(route(row, config, self.lookup).reason, "excluded")
        bot = message(channel=MENTION_ONLY, user=PLAIN, text=f"<@{OWNER}>", thread_ts=None,
                      is_bot=True)
        self.assertEqual(route(bot, self.config, self.lookup).reason, "excluded:bot")
        everything = replace(self.config, bot_messages=replace(self.config.bot_messages,
                                                                policy="route_all"))
        bot["text"] = "dummy"
        self.assertEqual(route(bot, everything, self.lookup).reason, NO_MENTION)

    def test_channels_without_the_flag_are_unchanged(self):
        for channel, rule in (("C0000000001", "channel_default"), ("C0000000002", "none")):
            with self.subTest(channel=channel):
                row = message(channel=channel, user=PLAIN, text="dummy", thread_ts=None)
                self.assertEqual(route(row, self.config, self.lookup).rule, rule)
        unflagged = replace(self.config, mention_only_channels=())
        decision = route(message(channel=MENTION_ONLY, user=PLAIN, text="dummy", thread_ts=None),
                         unflagged, self.lookup)
        self.assertEqual((decision.lane, decision.rule), (DEFAULT_LANE, "channel_default"))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-mention-only-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = fixture_config(self.root)
        self.store = LedgerStore(self.config.ledger.path)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id, session_pointer=lane.session, paused=lane.paused)
        self.lane, self.human = Mock(), Mock()
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(),
                                 [FileInboxNotifier(self.config.inbox.root, self.store.get_message),
                                  self.lane, self.human],
                                 WakePolicyEngine(self.config.wake_policy, lambda: 1000.0),
                                 lambda: 1000.0)

    def handle(self, number, **changes):
        return self.pipeline.handle_event(event(number, **dict(
            {"channel": MENTION_ONLY, "text": "dummy text"}, **changes)))

    def notices(self, mid):
        return self.store._rows("SELECT kind FROM human_notices WHERE message_id=?", (mid,))

    def inbox_ids(self):
        ids = []
        for path in sorted((self.root / "inbox").glob("*.jsonl")):
            ids.extend(path.read_text(encoding="utf-8").splitlines())
        return "\n".join(ids)

    def test_root_without_mention_is_recorded_excluded_and_nothing_else(self):
        self.assertNotIn("excluded_no_mention", self.pipeline.snapshot())
        result = self.handle(1)
        row = self.store.get_message(result.message_id)
        self.assertEqual((result.state, row["state"], row["note"], row["lane"], row["attention"]),
                         ("excluded", "excluded", NO_MENTION, None, None))
        self.assertEqual(self.notices(result.message_id), [])
        self.assertNotIn(result.message_id, self.inbox_ids())
        self.lane.notify.assert_not_called()
        self.human.notify.assert_not_called()
        self.assertFalse((self.root / "state/human_queue.jsonl").exists())
        self.assertIsNone(self.store.lookup_binding("slack", MENTION_ONLY, row["ts"]))
        counters = self.pipeline.snapshot()
        self.assertEqual((counters["excluded_no_mention"], counters["human_pending"]), (1, 0))

    def test_owner_mention_root_is_assigned_and_binds_its_thread(self):
        result = self.handle(1, text=f"<@{OWNER}> dummy")
        row = self.store.get_message(result.message_id)
        self.assertEqual((result.state, result.lane, row["attention"]),
                         ("assigned", DEFAULT_LANE, "owner_mention"))
        self.assertIn(result.message_id, self.inbox_ids())
        self.assertEqual([notice["kind"] for notice in self.notices(result.message_id)],
                         ["attention:owner_mention"])
        self.assertEqual(self.store.lookup_binding("slack", MENTION_ONLY, row["ts"]), DEFAULT_LANE)
        reply = self.handle(2, thread_ts=row["ts"], is_thread_reply=True)
        reply_row = self.store.get_message(reply.message_id)
        self.assertEqual((reply.state, reply.lane, reply_row["route_reason"], reply_row["attention"]),
                         ("assigned", DEFAULT_LANE, "thread_inherit", None))
        self.assertEqual(self.notices(reply.message_id), [])
        self.assertNotIn("excluded_no_mention", self.pipeline.snapshot())

    def test_always_human_author_is_assigned_with_attention(self):
        result = self.handle(1, user=PROTECTED)
        row = self.store.get_message(result.message_id)
        self.assertEqual((result.state, result.lane, row["attention"]),
                         ("assigned", DEFAULT_LANE, "always_human"))

    def test_reply_in_unbound_thread_without_mention_is_excluded(self):
        result = self.handle(2, thread_ts="1000.000001", is_thread_reply=True)
        row = self.store.get_message(result.message_id)
        self.assertEqual((result.state, row["note"]), ("excluded", NO_MENTION))
        self.assertEqual(self.notices(result.message_id), [])
        self.lane.notify.assert_not_called()
        self.assertEqual(self.pipeline.snapshot()["excluded_no_mention"], 1)

    def test_channel_without_the_flag_routes_as_before(self):
        result = self.handle(1, channel="C0000000001")
        self.assertEqual((result.state, result.lane), ("assigned", "support"))
        queued = self.handle(2, channel="C0000000002", text="dummy text")
        self.assertEqual(queued.state, "human_pending")
        self.assertNotIn("excluded_no_mention", self.pipeline.snapshot())


if __name__ == "__main__":
    unittest.main()
