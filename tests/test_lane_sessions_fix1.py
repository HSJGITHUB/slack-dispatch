"""P21 fix1: acked Wake now, the default cap, already queued items, backfill dedupe and the
pre-wake journal snapshot (dummy ids only)."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from slack_dispatch import service
from slack_dispatch.inbox import cli
from slack_dispatch.inbox.files import read
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline
from slack_dispatch.router.classifier import NullSuggester
from tests.test_console_app import NOW
from tests.test_lane_sessions import LaneSessionCase
from tests.test_pipeline_integration import event, fixture_config


class WakeNowConsumerTests(unittest.TestCase):
    """service.consume_wake_requests and Pipeline.flush with a real pipeline, mock adapters."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-wake-now-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = fixture_config(self.root)
        self.store = LedgerStore(self.config.ledger.path)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id)
        self.now = 1000.0
        self.notifiers = [Mock(), Mock(), Mock()]
        for notifier in self.notifiers:
            notifier.service_dry_run = False
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(), self.notifiers,
                                 WakePolicyEngine(self.config.wake_policy, lambda: self.now),
                                 lambda: self.now)

    def item(self, number, state="acked", user="U0000000088"):
        mid = self.store.record_event(event(number, user=user)).message_id
        self.store.set_route(mid, "support", "test")
        if state != "assigned":
            self.store.set_state(mid, state)
        return mid

    def session_wakes(self):
        return [call.args for call in self.notifiers[1].notify.call_args_list]

    def test_acked_item_is_woken_by_wake_now(self):
        mid = self.item(1)
        self.store.request_wake("support", mid, "console", "wake_now")
        self.assertEqual(service.consume_wake_requests(self.pipeline, self.store), 1)
        self.assertEqual(self.session_wakes(), [("support", [mid])])
        self.assertEqual(self.pipeline.wake_now_ids, set())
        self.assertEqual(self.store.pending_wake_requests(), [])

    def test_assign_requests_still_wake_only_assigned_items(self):
        acked = self.item(1)
        self.store.request_wake("support", acked, "cli")
        self.assertEqual(self.store.pending_wake_requests()[0]["kind"], "assign")
        self.assertEqual(service.consume_wake_requests(self.pipeline, self.store), 0)
        assigned = self.item(2, state="assigned")
        self.store.request_wake("support", assigned, "cli")
        self.assertEqual(service.consume_wake_requests(self.pipeline, self.store), 1)
        self.assertEqual(self.session_wakes(), [("support", [assigned])])

    def test_debounced_acked_wake_passes_the_flush_once(self):
        first, second = self.item(1), self.item(2)
        for mid in (first, second):
            self.store.request_wake("support", mid, "console", "wake_now")
        self.assertEqual(service.consume_wake_requests(self.pipeline, self.store), 2)
        # The second item of the same author waits for the debounce, marked as Wake now.
        self.assertEqual(self.session_wakes(), [("support", [first])])
        self.assertEqual(self.pipeline.wake_now_ids, {second})
        self.now += 120
        self.pipeline.flush(self.now)
        self.assertEqual(self.session_wakes(), [("support", [first]), ("support", [second])])
        self.assertEqual(self.pipeline.wake_now_ids, set())

    def test_other_acked_paths_are_not_revived(self):
        mid = self.item(1)
        # A deferred wake of an item acked meanwhile, without a Wake now mark, is dropped.
        self.pipeline.wake_engine.pending[("support", "U0000000088", mid)] = self.now
        self.now += 1
        self.pipeline.flush(self.now)
        self.assertEqual(self.session_wakes(), [])
        self.assertEqual(self.pipeline._current_ids("support", [mid], "lane"), [])
        self.pipeline.wake_now_ids.add(mid)
        self.assertEqual(self.pipeline._current_ids("support", [mid], "lane"), [mid])

    def test_wake_switch_off_drops_the_mark(self):
        mid = self.item(1)
        switch = Path(self.config.wake_policy.state_dir) / "wake.off"
        switch.parent.mkdir(parents=True, exist_ok=True)
        switch.write_text("", encoding="utf-8")
        self.store.request_wake("support", mid, "console", "wake_now")
        service.consume_wake_requests(self.pipeline, self.store)
        self.assertEqual(self.pipeline.wake_now_ids, set())

    def test_mock_pipeline_without_the_set(self):
        mid = self.item(1)
        self.store.request_wake("support", mid, "console", "wake_now")
        pipeline = Mock(lock=threading.RLock(), spec=["lock", "wake_engine", "_send",
                                                      "_human_notice"])
        pipeline.wake_engine.offer.return_value = None
        self.assertEqual(service.consume_wake_requests(pipeline, self.store), 1)


class WakeLimitTests(LaneSessionCase):
    def wake(self, body=None):
        return self.post("/api/lanes/support/wake", body if body is not None else {})

    def test_default_selection_is_capped_newest_first(self):
        ids = [self.lane_item(offset) for offset in range(1, 26)]
        data = self.wake()
        self.assertEqual((data["queued"], data["total"], data["capped"], data["already_queued"]),
                         (20, 25, True, 0))
        self.assertEqual([row["message_id"] for row in self.wakes()], list(reversed(ids))[:20])
        # Pressing again skips the 20 still waiting and wakes the 5 older ones.
        data = self.wake()
        self.assertEqual((data["queued"], data["total"], data["capped"], data["already_queued"]),
                         (5, 5, False, 20))
        self.assertEqual([row["message_id"] for row in self.wakes()][20:], list(reversed(ids[:5])))

    def test_already_queued_items_are_skipped(self):
        first, second = self.lane_item(1), self.lane_item(2)
        self.store.request_wake("support", first, "console")  # an assign wake still pending
        data = self.wake()
        self.assertEqual((data["queued"], data["already_queued"]), (1, 1))
        self.assertEqual(data["wake_requests"], [self.wakes()[-1]["id"]])
        self.assertEqual(self.wakes()[-1]["message_id"], second)
        explicit = self.wake({"ids": [first]})
        self.assertEqual((explicit["queued"], explicit["already_queued"],
                          explicit["results"][0]["ok"], explicit["results"][0]["detail"]),
                         (0, 1, True, "already_queued"))

    def test_backfill_record_is_written_once(self):
        mid = self.lane_item(1)
        first = self.wake()
        self.assertEqual((first["records_written"], first["records_skipped"]), (1, 0))
        self.store.consume_wake_requests(first["wake_requests"])
        second = self.wake()
        self.assertEqual((second["queued"], second["records_written"], second["records_skipped"]),
                         (1, 0, 1))
        backfills = [row for row in read(str(self.inbox), "support", include_done=True)
                     if row["id"] == mid and row["kind"] == "backfill"]
        self.assertEqual(len(backfills), 1)


class JournalSnapshotTests(LaneSessionCase):
    def test_before_is_the_pre_wake_snapshot(self):
        mid = self.lane_item(1)
        before = self.store.message_snapshots([mid])
        self.store.set_state(mid, "assigned", note="dummy changed")
        undo, journal = cli.journal_wake(self.store, "support", [mid], [], by="console",
                                         undo_seconds=30, now=NOW, before=before)
        row = self.store.get_console_action(undo["action_id"])
        self.assertEqual(journal, "ok")
        self.assertIsNone(json.loads(row["before"])[0]["note"])
        self.assertEqual(json.loads(row["after"])[0]["note"], "dummy changed")

    def test_wake_journals_state_from_before_the_wake(self):
        mid = self.lane_item(1)
        expected = self.store.message_snapshots([mid])[mid]
        data = self.post("/api/lanes/support/wake", {})
        row = self.store.get_console_action(data["undo"]["action_id"])
        entry = json.loads(row["before"])[0]
        self.assertEqual({key: entry[key] for key in expected if key != "id"},
                         {key: value for key, value in expected.items() if key != "id"})


if __name__ == "__main__":
    unittest.main()
