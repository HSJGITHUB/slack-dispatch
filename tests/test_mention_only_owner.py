"""P30 mention-only owner mode and owner thread binding.

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
from slack_dispatch.router.engine import NO_MENTION, is_mention_only, is_mention_only_owner, route
from tests.fakes.fake_store import message
from tests.test_pipeline_integration import event, fixture_config
from tests.test_router_config import raw_config

MENTION_ONLY = "C0000000004"
NORMAL = "C0000000001"
NULL_DEFAULT = "C0000000002"
DEFAULT_LANE = "owner"
OTHER_LANE = "support"
OWNER = "U0000000093"
PROTECTED = "U0000000091"
PLAIN = "U0000000088"


class ConfigTests(unittest.TestCase):
    def test_true_false_and_owner_parse_into_the_two_tuples(self):
        raw = raw_config()
        raw["channel_defaults"][MENTION_ONLY] = {"default": DEFAULT_LANE, "mention_only": "owner"}
        raw["channel_defaults"][NULL_DEFAULT] = {"default": None, "mention_only": True}
        raw["channel_defaults"][NORMAL] = {"default": OTHER_LANE, "mention_only": False}
        config = parse_config(raw)
        self.assertEqual(config.mention_only_channels, (NULL_DEFAULT, MENTION_ONLY))
        self.assertEqual(config.mention_only_owner_channels, (MENTION_ONLY,))
        self.assertEqual(config.channel_defaults[MENTION_ONLY], DEFAULT_LANE)
        self.assertEqual(config.channel_defaults[NORMAL], OTHER_LANE)

    def test_sample_has_no_owner_mode_channel(self):
        config = parse_config(raw_config())
        self.assertEqual(config.mention_only_channels, (MENTION_ONLY,))
        self.assertEqual(config.mention_only_owner_channels, ())

    def test_loader_rejects_other_values(self):
        for value in ("Owner", "OWNER", "owner ", "yes", "true", "", 1, 0, None, ["owner"],
                      {"owner": True}):
            with self.subTest(value=value):
                raw = deepcopy(raw_config())
                raw["channel_defaults"][MENTION_ONLY] = {"default": DEFAULT_LANE,
                                                         "mention_only": value}
                with self.assertRaises(ConfigError) as caught:
                    parse_config(raw)
                text = str(caught.exception)
                self.assertIn("mention_only", text)
                self.assertIn("true, false or owner", text)

    def test_loader_rejects_owner_in_dm_defaults(self):
        raw = raw_config()
        raw["dm_defaults"]["D0000000001"] = {"default": DEFAULT_LANE, "mention_only": "owner"}
        with self.assertRaises(ConfigError) as caught:
            parse_config(raw)
        self.assertIn("does not support mention_only", str(caught.exception))


class RouteTests(unittest.TestCase):
    def setUp(self):
        base = parse_config(raw_config())
        self.owner_mode = replace(base, mention_only_owner_channels=(MENTION_ONLY,))
        self.true_mode = base
        self.lookup = Mock(return_value=None)

    def route(self, config, **changes):
        row = message(**dict({"channel": MENTION_ONLY, "user": PLAIN, "text": "dummy text",
                              "thread_ts": None}, **changes))
        return route(row, config, self.lookup)

    def test_mode_helpers(self):
        row = message(channel=MENTION_ONLY, thread_ts=None)
        self.assertTrue(is_mention_only_owner(row, self.owner_mode))
        self.assertFalse(is_mention_only_owner(row, self.true_mode))
        self.assertTrue(is_mention_only(row, self.true_mode))
        # An owner mode channel counts as mention-only even when only the owner tuple has it.
        only_owner = replace(self.owner_mode, mention_only_channels=())
        self.assertTrue(is_mention_only(row, only_owner))

    def test_protected_root_is_excluded_in_owner_mode(self):
        decision = self.route(self.owner_mode, user=PROTECTED)
        self.assertEqual((decision.lane, decision.reason, decision.rule, decision.attention),
                         (None, NO_MENTION, "excluded", None))

    def test_protected_root_still_routes_in_true_mode(self):
        decision = self.route(self.true_mode, user=PROTECTED)
        self.assertEqual((decision.lane, decision.rule, decision.attention),
                         (DEFAULT_LANE, "channel_default", "always_human"))

    def test_owner_mention_routes_as_before(self):
        for user in (PLAIN, PROTECTED):
            with self.subTest(user=user):
                decision = self.route(self.owner_mode, user=user, text=f"<@{OWNER}> dummy")
                self.assertEqual((decision.lane, decision.rule, decision.attention),
                                 (DEFAULT_LANE, "channel_default", "owner_mention"))

    def test_bound_reply_keeps_its_attention(self):
        self.lookup.return_value = OTHER_LANE
        decision = self.route(self.owner_mode, user=PROTECTED, ts="2000.000002",
                              thread_ts="2000.000001")
        self.assertEqual((decision.lane, decision.reason, decision.rule, decision.attention),
                         (OTHER_LANE, "thread_inherit", "thread_inherit", "always_human"))
        plain = self.route(self.owner_mode, ts="2000.000002", thread_ts="2000.000001")
        self.assertEqual((plain.lane, plain.reason, plain.attention),
                         (OTHER_LANE, "thread_inherit", None))

    def test_unbound_reply_is_excluded(self):
        for user in (PLAIN, PROTECTED):
            with self.subTest(user=user):
                decision = self.route(self.owner_mode, user=user, ts="2000.000002",
                                      thread_ts="2000.000001")
                self.assertEqual(decision.reason, NO_MENTION)


def root_ts(case, result):
    return case.store.get_message(result.message_id)["ts"]


class OwnerCase(unittest.TestCase):
    followup = "changed_after_ack"

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-mention-owner-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        base = fixture_config(self.root)
        self.config = self.configure(replace(
            base, wake_policy=replace(base.wake_policy, followup=self.followup)))
        self.store = LedgerStore(self.config.ledger.path)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id, session_pointer=lane.session, paused=lane.paused)
        self.lane, self.human = Mock(), Mock()
        self.now = 1000.0
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(),
                                 [FileInboxNotifier(self.config.inbox.root, self.store.get_message),
                                  self.lane, self.human],
                                 WakePolicyEngine(self.config.wake_policy, lambda: self.now),
                                 lambda: self.now)

    def configure(self, config):
        return replace(config, mention_only_owner_channels=(MENTION_ONLY,))

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

    def assert_excluded_quietly(self, result):
        row = self.store.get_message(result.message_id)
        self.assertEqual((result.state, row["state"], row["note"], row["lane"]),
                         ("excluded", "excluded", NO_MENTION, None))
        self.assertEqual(self.notices(result.message_id), [])
        self.assertNotIn(result.message_id, self.inbox_ids())
        self.lane.notify.assert_not_called()
        self.human.notify.assert_not_called()
        self.assertFalse((self.root / "state/human_queue.jsonl").exists())


class OwnerModeGateTests(OwnerCase):
    def test_protected_root_is_excluded_with_nothing_else(self):
        result = self.handle(1, user=PROTECTED)
        self.assert_excluded_quietly(result)
        self.assertIsNone(self.store.get_message(result.message_id)["attention"])
        self.assertIsNone(self.store.lookup_binding("slack", MENTION_ONLY, "1000.000001"))
        counters = self.pipeline.snapshot()
        self.assertEqual((counters["excluded_no_mention"], counters["human_pending"]), (1, 0))
        self.assertNotIn("owner_threads_bound", counters)

    def test_owner_mention_is_assigned_and_binds_the_thread(self):
        result = self.handle(1, user=PROTECTED, text=f"<@{OWNER}> dummy")
        row = self.store.get_message(result.message_id)
        self.assertEqual((result.state, result.lane, row["attention"]),
                         ("assigned", DEFAULT_LANE, "owner_mention"))
        self.assertIn(result.message_id, self.inbox_ids())
        self.assertEqual(self.store.lookup_binding("slack", MENTION_ONLY, row["ts"]), DEFAULT_LANE)
        # The binding comes from the routing path, not from an owner post.
        self.assertNotIn("owner_threads_bound", self.pipeline.snapshot())

    def test_protected_reply_in_a_bound_thread_carries_always_human(self):
        root = self.handle(1, text=f"<@{OWNER}> dummy")
        reply = self.handle(2, user=PROTECTED, thread_ts=root_ts(self, root), is_thread_reply=True)
        row = self.store.get_message(reply.message_id)
        self.assertEqual((reply.state, reply.lane, row["route_reason"], row["attention"]),
                         ("assigned", DEFAULT_LANE, "thread_inherit", "always_human"))
        self.assertIn(reply.message_id, self.inbox_ids())
        self.assertEqual([notice["kind"] for notice in self.notices(reply.message_id)],
                         ["attention:always_human"])

    def test_plain_reply_in_an_unbound_thread_is_excluded(self):
        result = self.handle(2, thread_ts="1000.000001", is_thread_reply=True)
        self.assert_excluded_quietly(result)
        self.assertEqual(self.pipeline.snapshot()["excluded_no_mention"], 1)

    def test_edit_without_an_owner_mention_stays_excluded(self):
        original = self.handle(1, user=PROTECTED)
        self.now += 5
        edited = self.handle(2, ts="1000.000001", user=PROTECTED, subtype="message_changed",
                             text="dummy text edited")
        self.assertEqual(edited.message_id, original.message_id)
        row = self.store.get_message(original.message_id)
        self.assertEqual((row["state"], row["note"], row["lane"]), ("excluded", NO_MENTION, None))
        self.assertEqual(self.notices(original.message_id), [])
        self.assertNotIn(original.message_id, self.inbox_ids())
        self.lane.notify.assert_not_called()
        self.human.notify.assert_not_called()


class OwnerModeEditAlwaysTests(OwnerCase):
    """Under wake_policy.followup always the edit is routed again and still excluded."""
    followup = "always"

    def test_edit_without_an_owner_mention_is_routed_and_stays_excluded(self):
        original = self.handle(1, user=PROTECTED)
        self.now += 5
        edited = self.handle(2, ts="1000.000001", user=PROTECTED, subtype="message_changed",
                             text="dummy text edited")
        self.assertEqual((edited.message_id, edited.state), (original.message_id, "excluded"))
        self.assert_excluded_quietly(edited)
        self.assertEqual(self.pipeline.snapshot()["excluded_no_mention"], 2)


class TrueModeCompatTests(OwnerCase):
    def configure(self, config):
        return config

    def test_protected_root_is_still_assigned_with_always_human(self):
        result = self.handle(1, user=PROTECTED)
        row = self.store.get_message(result.message_id)
        self.assertEqual((result.state, result.lane, row["attention"]),
                         ("assigned", DEFAULT_LANE, "always_human"))
        self.assertNotIn("excluded_no_mention", self.pipeline.snapshot())


class OwnerThreadBindingTests(OwnerCase):
    """Owner posts bind their thread in mention-only channels of both modes."""

    def configure(self, config):
        # MENTION_ONLY stays in `true` mode; NULL_DEFAULT becomes an owner mode channel
        # with a null default.
        return replace(config, mention_only_channels=(MENTION_ONLY, NULL_DEFAULT),
                       mention_only_owner_channels=(NULL_DEFAULT,))

    def assert_owner_post(self, result):
        row = self.store.get_message(result.message_id)
        self.assertEqual((result.state, row["state"], row["note"], row["route_reason"]),
                         ("done", "done", "owner_post", "owner_post"))
        self.assertEqual(self.notices(result.message_id), [])
        self.assertNotIn(result.message_id, self.inbox_ids())

    def test_owner_root_binds_the_default_lane_and_a_later_reply_inherits(self):
        post = self.handle(1, user=OWNER, text="dummy data request")
        self.assert_owner_post(post)
        self.assertEqual(self.store.lookup_binding("slack", MENTION_ONLY, "1000.000001"),
                         DEFAULT_LANE)
        self.lane.notify.assert_not_called()
        self.human.notify.assert_not_called()
        self.assertEqual(self.pipeline.snapshot()["owner_threads_bound"], 1)
        reply = self.handle(2, thread_ts="1000.000001", is_thread_reply=True)
        row = self.store.get_message(reply.message_id)
        self.assertEqual((reply.state, reply.lane, row["route_reason"], row["attention"]),
                         ("assigned", DEFAULT_LANE, "thread_inherit", None))
        self.assertIn(reply.message_id, self.inbox_ids())
        self.assertNotIn("excluded_no_mention", self.pipeline.snapshot())

    def test_owner_reply_binds_the_thread_root(self):
        root = self.handle(1, text="dummy team talk")
        self.assertEqual(root.state, "excluded")
        post = self.handle(2, user=OWNER, thread_ts="1000.000001", is_thread_reply=True)
        self.assert_owner_post(post)
        self.assertEqual(self.store.lookup_binding("slack", MENTION_ONLY, "1000.000001"),
                         DEFAULT_LANE)
        self.assertIsNone(self.store.lookup_binding("slack", MENTION_ONLY, "1000.000002"))
        # The excluded root is not routed again.
        self.assertEqual(self.store.get_message(root.message_id)["state"], "excluded")
        reply = self.handle(3, thread_ts="1000.000001", is_thread_reply=True)
        self.assertEqual((reply.state, reply.lane), ("assigned", DEFAULT_LANE))

    def test_an_existing_binding_is_kept(self):
        self.store.bind_thread("slack", MENTION_ONLY, "1000.000001", OTHER_LANE)
        self.handle(2, user=OWNER, thread_ts="1000.000001", is_thread_reply=True)
        self.assertEqual(self.store.lookup_binding("slack", MENTION_ONLY, "1000.000001"),
                         OTHER_LANE)
        self.assertNotIn("owner_threads_bound", self.pipeline.snapshot())
        reply = self.handle(3, thread_ts="1000.000001", is_thread_reply=True)
        self.assertEqual((reply.state, reply.lane), ("assigned", OTHER_LANE))

    def test_a_null_default_binds_nothing(self):
        post = self.handle(1, channel=NULL_DEFAULT, user=OWNER)
        self.assert_owner_post(post)
        self.assertIsNone(self.store.lookup_binding("slack", NULL_DEFAULT, "1000.000001"))
        self.assertNotIn("owner_threads_bound", self.pipeline.snapshot())
        reply = self.handle(2, channel=NULL_DEFAULT, thread_ts="1000.000001",
                            is_thread_reply=True)
        self.assertEqual((reply.state, self.store.get_message(reply.message_id)["note"]),
                         ("excluded", NO_MENTION))

    def test_an_owner_post_in_a_normal_channel_binds_nothing(self):
        post = self.handle(1, channel=NORMAL, user=OWNER)
        self.assert_owner_post(post)
        self.assertIsNone(self.store.lookup_binding("slack", NORMAL, "1000.000001"))
        self.assertNotIn("owner_threads_bound", self.pipeline.snapshot())

    def test_a_reconcile_owner_post_also_binds(self):
        post = self.handle(1, user=OWNER, origin="reconcile")
        self.assert_owner_post(post)
        self.assertEqual(self.store.lookup_binding("slack", MENTION_ONLY, "1000.000001"),
                         DEFAULT_LANE)
        self.assertEqual(self.pipeline.snapshot()["owner_threads_bound"], 1)

    def test_owner_mode_channel_binds_too(self):
        config = replace(self.config, channel_defaults=dict(self.config.channel_defaults,
                                                            **{NULL_DEFAULT: OTHER_LANE}))
        self.pipeline.config = config
        self.handle(1, channel=NULL_DEFAULT, user=OWNER)
        self.assertEqual(self.store.lookup_binding("slack", NULL_DEFAULT, "1000.000001"),
                         OTHER_LANE)
        reply = self.handle(2, channel=NULL_DEFAULT, user=PROTECTED, thread_ts="1000.000001",
                            is_thread_reply=True)
        row = self.store.get_message(reply.message_id)
        self.assertEqual((reply.state, reply.lane, row["route_reason"], row["attention"]),
                         ("assigned", OTHER_LANE, "thread_inherit", "always_human"))

    def test_an_excluded_channel_binds_nothing(self):
        self.pipeline.config = replace(self.config, excluded_channels=(MENTION_ONLY,))
        self.handle(1, user=OWNER)
        self.assertIsNone(self.store.lookup_binding("slack", MENTION_ONLY, "1000.000001"))

    def test_a_second_owner_post_counts_once(self):
        self.handle(1, user=OWNER)
        self.handle(2, user=OWNER, thread_ts="1000.000001", is_thread_reply=True)
        self.assertEqual(self.pipeline.snapshot()["owner_threads_bound"], 1)


if __name__ == "__main__":
    unittest.main()
