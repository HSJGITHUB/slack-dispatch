"""P29: no human notice for attention items a lane already holds.

`human_queue.assigned_notice_flags` lists the attention flags for which an item routed to
a lane (state `assigned`) still queues a human notice. Offline only: the real SQLite ledger
in a temporary directory, file and mock notifiers and a fixed clock. Dummy ids only.
"""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from slack_dispatch.config.loader import ConfigError, parse_config
from slack_dispatch.config.schema import HumanQueue
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify import build_notifiers
from slack_dispatch.notify.file_drop import FileDropNotifier
from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import (Pipeline, assigned_notice_dropped, assigned_notice_flags,
                                     attention_only, queue_human_notice)
from slack_dispatch.router.classifier import NullSuggester
from tests.test_cards_writer import card_config
from tests.test_pipeline_integration import event, fixture_config
from tests.test_router_config import raw_config

OWNER = "U0000000093"
PROTECTED = "U0000000091"
RESTRICTIVE = ("always_human",)
OWNER_TO_LANE = f"[support] <@{OWNER}> dummy fyi"


class NoticeCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p29-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = 1000.0
        self.store = None

    def build(self, flags=None, *, mode="immediate", immediate_flags=None, followup=None):
        config = fixture_config(self.root)
        queue = replace(config.human_queue, mode=mode, digest_seconds=10)
        if flags is not None:
            queue = replace(queue, assigned_notice_flags=tuple(flags))
        if immediate_flags is not None:
            queue = replace(queue, immediate_flags=tuple(immediate_flags))
        config = replace(config, human_queue=queue)
        if followup is not None:
            config = replace(config, wake_policy=replace(config.wake_policy, followup=followup))
        self.config = config
        if self.store is None:
            self.store = LedgerStore(config.ledger.path, store_body=True)
            self.store.open()
            self.store.migrate()
            self.addCleanup(self.store.close)
            for lane in config.lanes:
                self.store.upsert_lane(lane.id, session_pointer=lane.session, paused=lane.paused)
        self.lane = Mock()
        notifiers = [FileInboxNotifier(config.inbox.root, self.store.get_message), self.lane,
                     FileDropNotifier(self.root / "human", config.notify_adapters[2].template)]
        clock = lambda: self.now  # noqa: E731
        engine = WakePolicyEngine(config.wake_policy, clock)
        self.offers = Mock(side_effect=engine.offer)
        engine.offer = self.offers
        self.pipeline = Pipeline(config, self.store, NullSuggester(), notifiers, engine, clock)
        return self.pipeline

    def drops(self):
        folder = self.root / "human"
        return [json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(folder.glob("*.json"))] if folder.exists() else []

    def notice_rows(self):
        return self.store._rows("SELECT message_id, kind, sent_at FROM human_notices "
                                "ORDER BY message_id, kind")

    def inbox_ids(self, lane):
        path = Path(self.config.inbox.root) / f"{lane}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["id"] for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]


class AssignedAttentionTests(NoticeCase):
    def test_default_config_keeps_the_immediate_fyi_notice(self):
        pipeline = self.build()
        self.assertEqual(self.config.human_queue.assigned_notice_flags,
                         ("always_human", "owner_mention"))
        result = pipeline.handle_event(event(text=OWNER_TO_LANE))
        self.assertEqual((result.state, result.lane), ("assigned", "support"))
        payload, = self.drops()
        self.assertIn("FYI, assigned to lane support", payload["body"])
        self.assertTrue(payload["body"].startswith("Attention notice: 1 item(s)"))
        self.assertEqual(payload["ids"], [result.message_id])
        self.assertNotIn("attention_notice_skipped", pipeline.snapshot())

    def test_restrictive_config_skips_owner_mention_notice_but_keeps_lane_delivery(self):
        pipeline = self.build(RESTRICTIVE)
        result = pipeline.handle_event(event(text=OWNER_TO_LANE))
        self.assertEqual((result.state, result.lane), ("assigned", "support"))
        self.assertEqual(self.notice_rows(), [])
        self.assertEqual(self.drops(), [])
        pipeline.flush(self.now + 4000)
        self.assertEqual(self.drops(), [])
        row = self.store.get_message(result.message_id)
        self.assertEqual(row["attention"], "owner_mention")
        self.assertEqual(row["state"], "assigned")
        # The lane inbox record and the wake offer happen as before.
        self.assertEqual(self.inbox_ids("support"), [result.message_id])
        self.offers.assert_called_once()
        self.assertEqual(self.offers.call_args.args[0], "support")
        self.lane.notify.assert_called_once_with("support", [result.message_id])
        self.assertEqual(pipeline.snapshot()["attention_notice_skipped"], 1)

    def test_lane_side_effects_match_the_default_config(self):
        calls = {}
        for flags in (None, RESTRICTIVE):
            self.store = None
            self.root = self.root / ("default" if flags is None else "restrictive")
            self.root.mkdir()
            pipeline = self.build(flags)
            result = pipeline.handle_event(event(text=OWNER_TO_LANE))
            pipeline.flush(self.now + 60)
            calls[flags] = (result.state, result.lane, self.inbox_ids("support"),
                            self.lane.notify.call_args_list, self.offers.call_count)
            self.root = self.root.parent
        self.assertEqual(calls[None], calls[RESTRICTIVE])

    def test_always_human_post_routed_to_a_lane_still_notifies(self):
        pipeline = self.build(RESTRICTIVE)
        result = pipeline.handle_event(event(user=PROTECTED, text="[support] dummy request"))
        self.assertEqual((result.state, result.lane), ("assigned", "support"))
        self.assertEqual(self.store.get_message(result.message_id)["attention"], "always_human")
        payload, = self.drops()
        self.assertIn("FYI, assigned to lane support", payload["body"])
        self.assertEqual(payload["ids"], [result.message_id])

    def test_owner_mention_in_the_human_queue_still_notifies_immediately(self):
        pipeline = self.build(RESTRICTIVE)
        result = pipeline.handle_event(event(text=f"<@{OWNER}> dummy urgent"))
        self.assertEqual(result.state, "human_pending")
        payload, = self.drops()
        self.assertTrue(payload["body"].startswith("Human queue notice: 1 item(s)"))
        self.assertEqual(payload["ids"], [result.message_id])
        self.assertNotIn("attention_notice_skipped", pipeline.snapshot())

    def test_edit_of_an_assigned_owner_mention_queues_nothing(self):
        pipeline = self.build(RESTRICTIVE, followup="always")
        result = pipeline.handle_event(event(text=OWNER_TO_LANE))
        self.now += 5
        edited = pipeline.handle_event(event(2, ts="1000.000001", subtype="message_changed",
                                             text=OWNER_TO_LANE + " edited"))
        self.assertEqual(edited.message_id, result.message_id)
        self.assertEqual((edited.state, edited.lane), ("assigned", "support"))
        # The edit took the edit-on-assigned path (a second wake offer).
        self.assertEqual(self.offers.call_count, 2)
        self.assertEqual(self.notice_rows(), [])
        self.assertEqual(self.drops(), [])
        self.assertEqual(self.store.get_message(result.message_id)["attention"], "owner_mention")
        self.assertEqual(pipeline.snapshot()["attention_notice_skipped"], 2)

    def test_edit_of_an_assigned_owner_mention_notifies_under_the_default(self):
        pipeline = self.build(followup="always")
        pipeline.handle_event(event(text=OWNER_TO_LANE))
        self.now += 5
        pipeline.handle_event(event(2, ts="1000.000001", subtype="message_changed",
                                    text=OWNER_TO_LANE + " edited"))
        self.assertEqual(self.offers.call_count, 2)
        # One notice per message and kind (one-shot), as before P29.
        self.assertEqual(len(self.notice_rows()), 1)
        self.assertEqual(len(self.drops()), 1)


class FlushDropTests(NoticeCase):
    def assigned_owner_mention(self):
        pipeline = self.build(RESTRICTIVE)
        result = pipeline.handle_event(event(text=OWNER_TO_LANE))
        self.assertEqual(self.notice_rows(), [])
        queue_human_notice(self.config, self.store, result.message_id, "attention:owner_mention",
                           immediate=False, now=self.now)
        self.assertEqual(len(self.notice_rows()), 1)
        return result.message_id

    def test_prequeued_row_is_dropped_under_the_restrictive_config(self):
        self.assigned_owner_mention()
        self.pipeline.flush(self.now + 4000)
        self.assertEqual(self.notice_rows(), [])
        self.assertEqual(self.drops(), [])
        self.assertEqual(self.pipeline.snapshot()["attention_notice_skipped"], 2)

    def test_prequeued_row_is_sent_under_the_default_config(self):
        mid = self.assigned_owner_mention()
        pipeline = self.build()  # a restart with the default configuration
        pipeline.flush(self.now + 4000)
        [row] = self.notice_rows()
        self.assertIsNotNone(row["sent_at"])
        payload, = self.drops()
        self.assertEqual(payload["ids"], [mid])
        self.assertTrue(payload["body"].startswith("Attention notice: 1 item(s)"))

    def test_human_pending_rows_are_unaffected(self):
        pipeline = self.build(RESTRICTIVE, mode="digest", immediate_flags=())
        result = pipeline.handle_event(event(text=f"<@{OWNER}> dummy urgent"))
        self.assertEqual(result.state, "human_pending")
        self.assertEqual([row["kind"] for row in self.notice_rows()], ["attention:owner_mention"])
        pipeline.flush(self.now + 60)
        payload, = self.drops()
        self.assertEqual(payload["ids"], [result.message_id])
        self.assertNotIn("attention_notice_skipped", pipeline.snapshot())

    def test_drop_rule(self):
        config = SimpleConfig(RESTRICTIVE)
        self.assertTrue(assigned_notice_dropped(config, "attention:owner_mention", "assigned"))
        self.assertFalse(assigned_notice_dropped(config, "attention:always_human", "assigned"))
        self.assertFalse(assigned_notice_dropped(config, "attention:owner_mention", "human_pending"))
        for kind in ("message", "storm", "reassign", "unassigned"):
            self.assertFalse(assigned_notice_dropped(config, kind, "assigned"))
        default = SimpleConfig(None)
        self.assertEqual(assigned_notice_flags(default), ("always_human", "owner_mention"))
        self.assertFalse(assigned_notice_dropped(default, "attention:owner_mention", "assigned"))


class SimpleConfig:
    """A config with only a human_queue, or none (older callers without the key)."""
    def __init__(self, flags):
        if flags is not None:
            self.human_queue = HumanQueue(assigned_notice_flags=flags)


class CardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p29-cards-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = 1000.0
        config = card_config(self.root)
        self.config = replace(config, human_queue=replace(config.human_queue,
                                                          assigned_notice_flags=RESTRICTIVE))
        self.store = LedgerStore(self.config.ledger.path, store_body=True)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id, session_pointer=lane.session, paused=lane.paused)
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(),
                                 build_notifiers(self.config, self.store),
                                 WakePolicyEngine(self.config.wake_policy, lambda: self.now),
                                 lambda: self.now)

    def files(self, folder):
        path = self.root / folder
        return sorted(path.glob("*.json")) if path.exists() else []

    def test_human_pending_owner_mention_keeps_notice_and_card(self):
        result = self.pipeline.handle_event(event(text=f"<@{OWNER}> dummy urgent"))
        self.assertEqual(result.state, "human_pending")
        [card] = self.files("cards_dir/cards")
        self.assertEqual(json.loads(card.read_text(encoding="utf-8"))["message_id"],
                         result.message_id)
        [notice] = self.files("human")
        self.assertTrue(json.loads(notice.read_text(encoding="utf-8"))["body"].startswith(
            "Human queue notice: 1 item(s)"))

    def test_assigned_owner_mention_gives_neither_notice_nor_card(self):
        result = self.pipeline.handle_event(event(text=OWNER_TO_LANE))
        self.assertEqual(result.state, "assigned")
        self.assertEqual(self.files("cards_dir/cards"), [])
        self.assertEqual(self.files("human"), [])
        self.assertEqual(self.store._rows("SELECT * FROM human_notices"), [])


class TitleTests(NoticeCase):
    def test_only_assigned_items_are_titled_attention_notice_in_a_digest(self):
        pipeline = self.build(mode="digest", immediate_flags=())
        first = pipeline.handle_event(event(1, text=OWNER_TO_LANE))
        second = pipeline.handle_event(event(2, user=PROTECTED, text="[support] dummy request"))
        self.assertEqual({first.state, second.state}, {"assigned"})
        pipeline.flush(self.now + 60)
        payload, = self.drops()
        self.assertTrue(payload["body"].startswith("Attention notice: 2 item(s)"))
        self.assertIn("FYI, assigned to lane support", payload["body"])

    def test_a_batch_with_a_human_pending_item_keeps_the_queue_title(self):
        pipeline = self.build(mode="digest", immediate_flags=())
        pipeline.handle_event(event(1, text=OWNER_TO_LANE))
        pending = pipeline.handle_event(event(2, text="ordinary dummy item"))
        self.assertEqual(pending.state, "human_pending")
        pipeline.flush(self.now + 60)
        payload, = self.drops()
        self.assertTrue(payload["body"].startswith("Human queue digest: 2 item(s)"))
        self.assertIn("FYI, assigned to lane support", payload["body"])

    def test_an_immediate_human_pending_notice_keeps_the_queue_title(self):
        pipeline = self.build()
        pipeline.handle_event(event(text=f"<@{OWNER}> dummy urgent"))
        payload, = self.drops()
        self.assertTrue(payload["body"].startswith("Human queue notice: 1 item(s)"))

    def test_attention_only_needs_attention_rows_of_assigned_items(self):
        assigned = {"slack:C1:1.1": {"state": "assigned"}}
        pending = {"slack:C1:1.1": {"state": "assigned"}, "slack:C1:1.2": {"state": "human_pending"}}
        self.assertTrue(attention_only([{"kind": "attention:owner_mention"}], assigned))
        self.assertFalse(attention_only([{"kind": "attention:owner_mention"},
                                         {"kind": "message"}], pending))
        # Timer and storm notices about assigned items keep the human queue title.
        for kind in ("reassign", "storm", "unassigned"):
            self.assertFalse(attention_only([{"kind": kind}], assigned))
        self.assertFalse(attention_only([], {}))


class LoaderTests(unittest.TestCase):
    def test_absent_key_gives_the_default(self):
        raw = raw_config()
        raw["human_queue"].pop("assigned_notice_flags", None)
        self.assertEqual(parse_config(raw).human_queue.assigned_notice_flags,
                         ("always_human", "owner_mention"))
        raw.pop("human_queue")
        self.assertEqual(parse_config(raw).human_queue.assigned_notice_flags,
                         ("always_human", "owner_mention"))

    def test_parses_the_key(self):
        for value in (["always_human"], [], ["owner_mention", "always_human"]):
            with self.subTest(value=value):
                raw = raw_config()
                raw["human_queue"]["assigned_notice_flags"] = value
                self.assertEqual(parse_config(raw).human_queue.assigned_notice_flags, tuple(value))

    def test_rejects_unknown_flags_and_non_lists(self):
        for value in (["always_human", "dummy_flag"], [None], [["owner_mention"]],
                      "always_human", None, {"always_human": True}, 1):
            with self.subTest(value=value):
                raw = deepcopy(raw_config())
                raw["human_queue"]["assigned_notice_flags"] = value
                with self.assertRaises(ConfigError) as caught:
                    parse_config(raw)
                self.assertIn("human_queue.assigned_notice_flags", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
