import json
import unittest
from unittest.mock import mock_open, patch

from slack_dispatch.queue.human import assign, export_jsonl, list_pending, promotion_stats
from tests.fakes.fake_store import FakeStore, message


class QueueTests(unittest.TestCase):
    def test_pending_metadata_limit_age(self):
        store = FakeStore([message(state="human_pending", classifier_suggestion="support",
                                   suggestion_reason="Relevant.", body="PRIVATE"),
                           message(id="other", state="assigned")])
        items = list_pending(store, 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].suggested_lane, "support")
        self.assertEqual(items[0].reason, "Relevant.")
        self.assertGreater(items[0].age_seconds, 0)
        self.assertNotIn("PRIVATE", repr(items))
        self.assertEqual(list_pending(store, 0), [])

    def test_assign_reply_note_and_retained_suggestion(self):
        row = message(state="human_pending", thread_ts="1", classifier_suggestion="content")
        store = FakeStore([row])
        assign(store, row["id"], "support", by="reviewer-placeholder", note="Reviewed.")
        updated = store.get_message(row["id"])
        self.assertEqual(updated["state"], "assigned")
        self.assertEqual(updated["route_reason"], "human:reviewer-placeholder")
        self.assertEqual(updated["note"], "Reviewed.")
        self.assertEqual(updated["classifier_suggestion"], "content")
        self.assertEqual(store.lookup_binding("slack", row["channel"], "1"), "support")
        with self.assertRaises(KeyError):
            assign(store, "missing", "support", by="reviewer-placeholder", note=None)

    def test_assign_root_without_note(self):
        row = message()
        store = FakeStore([row])
        assign(store, row["id"], "support", by="reviewer-placeholder", note=None)
        self.assertFalse(store.bindings)
        self.assertNotIn("note", store.get_message(row["id"]))

    def test_export_only_ids_and_suggestions_all_rows(self):
        rows = [message(id=f"dummy:{i}", state="human_pending", classifier_suggestion="support",
                        text="PRIVATE", body="PRIVATE", suggestion_reason="PRIVATE")
                for i in range(205)]
        store = FakeStore(rows + [message(id="assigned", state="assigned")])
        output = mock_open()
        with patch("slack_dispatch.queue.human.Path.open", output):
            export_jsonl(store, "dummy.jsonl")
        text = "".join(call.args[0] for call in output().write.call_args_list)
        records = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(len(records), 205)
        self.assertTrue(all(set(row) == {"id", "suggested_lane"} for row in records))
        self.assertNotIn("PRIVATE", text)

    def test_promotion_thresholds_and_channel_scope(self):
        rows = [message(id=f"dummy:{i}", state="done", lane="support",
                        route_reason="human:reviewer-placeholder",
                        classifier_suggestion="support" if i < 27 else "content")
                for i in range(30)]
        store = FakeStore(rows)
        stats = promotion_stats(store, rows[0]["channel"])
        self.assertEqual(stats, dict(judged=30, agreed=27, agreement=0.9, eligible=True))
        self.assertEqual(promotion_stats(store, "other")["judged"], 0)
        store.messages["dummy:29"]["route_reason"] = "channel_default"
        self.assertFalse(promotion_stats(store, rows[0]["channel"])["eligible"])
        store.messages["dummy:29"]["route_reason"] = "human:reviewer-placeholder"
        store.messages["dummy:0"]["classifier_suggestion"] = None
        self.assertFalse(promotion_stats(store, rows[0]["channel"])["eligible"])

    def test_promotion_not_truncated_at_default_limit(self):
        store = FakeStore([message(id=f"dummy:{i}", state="assigned", lane="support",
                                   route_reason="human:reviewer-placeholder",
                                   classifier_suggestion="support") for i in range(205)])
        self.assertEqual(promotion_stats(store, "C0000000001")["judged"], 205)
