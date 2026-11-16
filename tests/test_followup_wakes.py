"""P22: message_changed edits wake a lane only per wake_policy.followup."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import yaml

from slack_dispatch.config.loader import ConfigError, load_config, parse_config
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline, edit_text_changed, followup_policy
from slack_dispatch.router.classifier import NullSuggester
from tests.test_pipeline_integration import SAMPLE, event, fixture_config

TEXT = "[support] see https://example.invalid/dummy"
ROOT_TS = "1000.000001"


def edit(number, text=TEXT, **changes):
    """A message_changed event of the root message (a new event id, the same ts)."""
    changes.setdefault("ts", ROOT_TS)
    return event(number, subtype="message_changed", text=text, **changes)


class FollowupWakeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p22-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = fixture_config(self.root)
        self.now = 1000.0
        self.store = self.open_store(store_body=True)

    def open_store(self, *, store_body):
        store = LedgerStore(self.config.ledger.path, store_body=store_body)
        store.open()
        store.migrate()
        self.addCleanup(store.close)
        for lane in self.config.lanes:
            store.upsert_lane(lane.id, session_pointer=lane.session, paused=lane.paused)
        return store

    def make_pipeline(self, policy=None):
        if policy is not None:
            self.config = replace(self.config, wake_policy=replace(self.config.wake_policy,
                                                                   followup=policy))
        clock = lambda: self.now  # noqa: E731
        self.lane, self.human = Mock(), Mock()
        notifiers = [FileInboxNotifier(self.config.inbox.root, self.store.get_message),
                     self.lane, self.human]
        engine = WakePolicyEngine(self.config.wake_policy, clock)
        self.offers = Mock(side_effect=engine.offer)
        engine.offer = self.offers
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(), notifiers, engine, clock)
        return self.pipeline

    def records(self):
        path = self.root / "inbox/support.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def post_root(self, policy=None):
        self.make_pipeline(policy)
        result = self.pipeline.handle_event(event(text=TEXT))
        self.assertEqual((result.state, result.lane), ("assigned", "support"))
        self.assertEqual((self.offers.call_count, self.lane.notify.call_count, len(self.records())),
                         (1, 1, 1))
        self.now += 100  # past the wake debounce, inside the followup window
        return result.message_id

    def row(self, mid):
        return self.store.get_message(mid, include_body=True)

    def test_unfurl_only_edits_while_assigned_update_the_ledger_silently(self):
        mid = self.post_root()
        with self.assertLogs("slack_dispatch.pipeline", level="INFO") as logs:
            result = self.pipeline.handle_event(edit(2, text=TEXT + "\n"))
        self.assertEqual((result.state, result.lane), ("assigned", "support"))
        row = self.row(mid)
        self.assertEqual((row["body"], row["state"]), (TEXT + "\n", "assigned"))
        self.assertIsNotNone(row["edited_at"])
        self.assertEqual((self.offers.call_count, self.lane.notify.call_count, len(self.records())),
                         (1, 1, 1))
        self.human.notify.assert_not_called()
        self.assertEqual(self.pipeline.edits_ignored, 1)
        snapshot = self.pipeline.snapshot()
        self.assertEqual((snapshot["edits_ignored"], snapshot["edits_woken"]), (1, 0))
        self.assertEqual(logs.output,
                         ["INFO:slack_dispatch.pipeline:Edit recorded without wake id=" + mid])
        # The observed case: further link previews attach within seconds.
        for number in (3, 4):
            self.now += 10
            self.pipeline.handle_event(edit(number))
        self.assertEqual((self.offers.call_count, len(self.records()), self.pipeline.edits_ignored),
                         (1, 1, 3))

    def test_text_edit_while_assigned_does_not_wake(self):
        mid = self.post_root()
        self.pipeline.handle_event(edit(2, text=TEXT + " plus a correction"))
        self.assertEqual(self.row(mid)["body"], TEXT + " plus a correction")
        self.assertEqual(self.row(mid)["state"], "assigned")
        self.assertEqual((self.offers.call_count, len(self.records())), (1, 1))
        self.assertEqual((self.pipeline.edits_ignored, self.pipeline.edits_woken), (1, 0))

    def test_text_edit_after_ack_wakes_once_per_window(self):
        mid = self.post_root()
        self.store.set_state(mid, "acked")
        self.pipeline.handle_event(edit(2, text="[support] second version"))
        self.assertEqual((self.offers.call_count, self.lane.notify.call_count), (2, 2))
        self.assertEqual(self.offers.call_args.args[1]["id"], mid)
        last = self.records()[-1]
        self.assertEqual((len(self.records()), last["id"], last["kind"], last["followup"]),
                         (2, mid, "followup", True))
        self.assertEqual(self.row(mid)["state"], "assigned")
        self.assertEqual((self.pipeline.edits_woken, self.pipeline.edits_ignored), (1, 0))
        # A second text edit inside the window: no second wake, while assigned or after ack.
        self.now += 100
        self.pipeline.handle_event(edit(3, text="[support] third version"))
        self.store.set_state(mid, "acked")
        self.now += 100
        self.pipeline.handle_event(edit(4, text="[support] fourth version"))
        self.assertEqual((self.offers.call_count, len(self.records())), (2, 2))
        self.assertEqual(self.row(mid)["state"], "acked")
        self.assertEqual(self.row(mid)["body"], "[support] fourth version")
        # An unfurl after ack is not a text change.
        self.now += self.config.service.followup_window_seconds
        self.pipeline.handle_event(edit(5, text="[support] fourth version"))
        self.assertEqual(self.offers.call_count, 2)
        # Past the window a new text change wakes again.
        self.pipeline.handle_event(edit(6, text="[support] fifth version"))
        self.assertEqual((self.offers.call_count, len(self.records())), (3, 3))
        self.assertEqual((self.pipeline.edits_woken, self.pipeline.edits_ignored), (2, 3))

    def test_window_boundary_is_inclusive(self):
        mid = self.post_root()
        self.store.set_state(mid, "acked")
        self.pipeline.handle_event(edit(2, text="[support] second version"))
        self.store.set_state(mid, "acked")
        self.now += self.config.service.followup_window_seconds
        self.pipeline.handle_event(edit(3, text="[support] third version"))
        self.assertEqual(self.offers.call_count, 2)
        self.now += 1
        self.pipeline.handle_event(edit(4, text="[support] fourth version"))
        self.assertEqual(self.offers.call_count, 3)

    def test_done_item_text_edit_reopens_to_assigned_and_wakes(self):
        mid = self.post_root()
        self.store.set_state(mid, "done")
        self.pipeline.handle_event(edit(2, text=TEXT))
        self.assertEqual((self.row(mid)["state"], self.offers.call_count), ("done", 1))
        self.pipeline.handle_event(edit(3, text="[support] reopened by a change"))
        self.assertEqual((self.row(mid)["state"], self.offers.call_count), ("assigned", 2))
        self.assertEqual(self.records()[-1]["kind"], "followup")

    def test_policy_always_reproduces_the_old_behaviour(self):
        mid = self.post_root("always")
        self.pipeline.handle_event(edit(2))
        self.assertEqual((self.offers.call_count, self.lane.notify.call_count), (2, 2))
        self.assertEqual((self.records()[-1]["kind"], self.records()[-1]["followup"]),
                         ("followup", True))
        self.now += 10
        self.pipeline.handle_event(edit(3))
        self.assertEqual((self.offers.call_count, len(self.records())), (3, 3))
        self.assertEqual(self.row(mid)["state"], "assigned")
        self.assertEqual((self.pipeline.edits_woken, self.pipeline.edits_ignored), (2, 0))

    def test_policy_never_wakes_nothing(self):
        mid = self.post_root("never")
        self.store.set_state(mid, "acked")
        self.pipeline.handle_event(edit(2, text="[support] changed"))
        self.store.set_state(mid, "done")
        self.pipeline.handle_event(edit(3, text="[support] changed again"))
        self.assertEqual((self.offers.call_count, len(self.records())), (1, 1))
        self.assertEqual((self.row(mid)["state"], self.row(mid)["body"]),
                         ("done", "[support] changed again"))
        self.assertEqual((self.pipeline.edits_woken, self.pipeline.edits_ignored), (0, 2))

    def test_reply_followup_path_unchanged(self):
        root = self.post_root()
        reply = self.pipeline.handle_event(event(2, text="plain reply", thread_ts=ROOT_TS,
                                                 ts="1000.000002", is_thread_reply=True))
        self.assertEqual(reply.lane, "support")
        self.assertTrue(self.store.get_message(reply.message_id)["followup"])
        self.assertEqual([(row["kind"], row["followup"]) for row in self.records()],
                         [("new", False), ("new", True)])
        self.assertEqual(self.offers.call_count, 2)
        self.assertEqual(self.offers.call_args.args[1]["id"], reply.message_id)
        self.assertNotEqual(root, reply.message_id)
        snapshot = self.pipeline.snapshot()
        self.assertNotIn("edits_ignored", snapshot)
        self.assertNotIn("edits_woken", snapshot)

    def queue_untagged(self):
        self.make_pipeline()
        result = self.pipeline.handle_event(event(text="no tag here"))
        self.assertEqual(result.state, "human_pending")
        self.assertEqual(self.human.notify.call_count, 1)
        self.now += 100
        return result.message_id

    def test_human_pending_text_edit_that_gains_a_tag_is_a_first_assignment(self):
        mid = self.queue_untagged()
        edited = self.pipeline.handle_event(edit(2, text="[support] now tagged <@U0000000093>"))
        self.assertEqual((edited.state, edited.lane), ("assigned", "support"))
        self.assertEqual((self.offers.call_count, self.lane.notify.call_count), (1, 1))
        self.assertEqual([row["id"] for row in self.records()], [mid])
        # The attention notice of the new routing goes out as usual.
        self.assertEqual(self.store.get_message(mid)["attention"], "owner_mention")
        self.assertEqual(self.human.notify.call_count, 2)
        self.assertEqual((self.pipeline.edits_woken, self.pipeline.edits_ignored), (0, 0))
        self.assertNotIn("edits_woken", self.pipeline.snapshot())
        # Outside the edit window: once acked, a further text edit wakes at once.
        self.store.set_state(mid, "acked")
        self.pipeline.handle_event(edit(3, text="[support] now tagged, corrected"))
        self.assertEqual((self.offers.call_count, self.pipeline.edits_woken), (2, 1))

    def test_human_pending_edit_that_stays_queued_records_only(self):
        mid = self.queue_untagged()
        pending = self.pipeline.counters["human_pending"]
        edited = self.pipeline.handle_event(edit(2, text="still no tag here"))
        self.assertEqual((edited.state, edited.lane), ("human_pending", None))
        self.assertEqual(self.row(mid)["body"], "still no tag here")
        self.assertEqual((self.human.notify.call_count, self.pipeline.counters["human_pending"]),
                         (1, pending))
        self.pipeline.flush(self.now + 10000)
        self.assertEqual(self.human.notify.call_count, 1)
        self.assertEqual(self.offers.call_count, 0)
        self.assertEqual(self.pipeline.edits_ignored, 1)
        # An unchanged text (an unfurl) is ignored without routing.
        routed = self.pipeline.counters["routed"]
        self.pipeline.handle_event(edit(3, text="still no tag here"))
        self.assertEqual((self.pipeline.counters["routed"], self.pipeline.edits_ignored),
                         (routed, 2))

    def test_edit_of_a_never_routed_item_routes_it(self):
        self.make_pipeline()
        mid = self.store.record_event(event(text=TEXT)).message_id  # recorded, never routed
        self.assertEqual(self.store.get_message(mid)["state"], "new")
        self.now += 100
        edited = self.pipeline.handle_event(edit(2))
        self.assertEqual((edited.state, edited.lane), ("assigned", "support"))
        self.assertEqual((self.offers.call_count, self.lane.notify.call_count), (1, 1))
        self.assertEqual([row["id"] for row in self.records()], [mid])
        self.assertEqual((self.pipeline.edits_woken, self.pipeline.edits_ignored), (0, 0))

    def test_missing_stored_body_counts_as_changed(self):
        self.store.close()
        self.root = self.root / "nobody"
        self.root.mkdir()
        self.config = fixture_config(self.root)
        self.store = self.open_store(store_body=False)
        mid = self.post_root()
        self.pipeline.handle_event(edit(2))
        self.assertEqual(self.offers.call_count, 1)  # assigned: never wakes
        self.store.set_state(mid, "acked")
        self.pipeline.handle_event(edit(3))  # same text, but no stored body to compare
        self.assertEqual((self.offers.call_count, self.records()[-1]["kind"]), (2, "followup"))
        self.assertIsNone(self.row(mid)["body"])

    def test_edit_of_unknown_message_does_not_count(self):
        self.make_pipeline()
        self.pipeline.handle_event(edit(2))
        self.assertEqual((self.pipeline.edits_ignored, self.pipeline.edits_woken), (0, 0))
        self.assertEqual(self.offers.call_count, 0)


class PreviousBodyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p22-store-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def store(self, store_body):
        store = LedgerStore(self.root / f"ledger-{store_body}.sqlite3", store_body=store_body)
        store.open()
        store.migrate()
        self.addCleanup(store.close)
        return store

    def test_record_event_returns_the_body_before_an_edit(self):
        store = self.store(True)
        self.assertIsNone(store.record_event(event(text="first")).previous_body)
        self.assertEqual(store.record_event(edit(2, text="second")).previous_body, "first")
        self.assertEqual(store.record_event(edit(3, text="third")).previous_body, "second")
        self.assertEqual(store.get_message("slack:C0000000002:" + ROOT_TS,
                                           include_body=True)["body"], "third")
        self.assertIsNone(store.record_event(edit(4, ts="1000.000009")).previous_body)
        duplicate = store.record_event(edit(3, text="third"))
        self.assertEqual((duplicate.duplicate, duplicate.previous_body), (True, None))

    def test_without_store_body_there_is_no_previous_body(self):
        store = self.store(False)
        store.record_event(event(text="first"))
        self.assertIsNone(store.record_event(edit(2, text="second")).previous_body)


class HelperAndConfigTests(unittest.TestCase):
    def test_edit_text_changed(self):
        self.assertFalse(edit_text_changed("dummy text", "  dummy text\n"))
        self.assertTrue(edit_text_changed("dummy text", "dummy text 2"))
        self.assertTrue(edit_text_changed(None, "dummy text"))
        self.assertTrue(edit_text_changed("dummy text", None))
        self.assertFalse(edit_text_changed("", " "))

    def test_config_default_sample_and_validation(self):
        raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(load_config(SAMPLE).wake_policy.followup, "changed_after_ack")
        raw["wake_policy"].pop("followup")
        self.assertEqual(parse_config(raw).wake_policy.followup, "changed_after_ack")
        for value in ("always", "never", "changed_after_ack"):
            raw["wake_policy"]["followup"] = value
            self.assertEqual(parse_config(raw).wake_policy.followup, value)
        for value in ("sometimes", "", None, 1, True, ["always"]):
            with self.subTest(value=value):
                raw["wake_policy"]["followup"] = value
                with self.assertRaises(ConfigError):
                    parse_config(raw)

    def test_followup_policy_fallback(self):
        self.assertEqual(followup_policy(Mock(wake_policy=Mock(followup="never"))), "never")
        self.assertEqual(followup_policy(Mock(wake_policy=object())), "changed_after_ack")
        self.assertEqual(followup_policy(object()), "changed_after_ack")


if __name__ == "__main__":
    unittest.main()
