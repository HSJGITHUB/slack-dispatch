"""Offline pipeline integration against the production SQLite ledger."""
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from slack_dispatch.config.loader import load_config
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline
from slack_dispatch.router.classifier import NullSuggester

SAMPLE = Path(__file__).resolve().parents[1] / "examples/instance-sample/config.yaml"


def fixture_config(root):
    config = load_config(SAMPLE)
    return replace(config,
                   ledger=replace(config.ledger, path=str(root / "ledger.sqlite3")),
                   service=replace(config.service, state_dir=str(root / "state"),
                                   log_dir=str(root / "logs")),
                   inbox=replace(config.inbox, root=str(root / "inbox")),
                   wake_policy=replace(config.wake_policy, state_dir=str(root / "state")),
                   notify_adapters=(
                       replace(config.notify_adapters[0], path=str(root / "inbox")),
                       replace(config.notify_adapters[-1], purpose="lane", path=str(root / "outbox")),
                       replace(config.notify_adapters[-1], purpose="human_queue", path=str(root / "human"))),
                   sources=())


def event(number=1, **changes):
    result = dict(source="slack", event_id=f"EvDUMMY{number}", event_type="message",
                  subtype=None, channel="C0000000002", channel_type="channel",
                  user="U0000000088", ts=f"1000.{number:06d}", thread_ts=None,
                  is_thread_reply=False, has_files=False,
                  received_at=datetime.fromtimestamp(1000, timezone.utc).isoformat(),
                  text="[support] private fixture text")
    result.update(changes)
    return result


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dispatch-pipeline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = fixture_config(self.root)
        self.store = LedgerStore(self.config.ledger.path)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id, session_pointer=lane.session, paused=lane.paused)
        self.now = 1000.0
        self.clock = lambda: self.now
        self.lane, self.human = Mock(), Mock()
        self.notifiers = [FileInboxNotifier(self.config.inbox.root, self.store.get_message),
                          self.lane, self.human]
        self.pipeline = self.make_pipeline()

    def make_pipeline(self):
        return Pipeline(self.config, self.store, NullSuggester(), self.notifiers,
                        WakePolicyEngine(self.config.wake_policy, self.clock), self.clock)

    def records(self):
        return [json.loads(line) for line in
                (self.root / "inbox/support.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_message_routes_using_text_without_persisting_it(self):
        result = self.pipeline.handle_event(event())
        self.assertEqual((result.state, result.lane), ("assigned", "support"))
        self.lane.notify.assert_called_once_with("support", [result.message_id])
        self.human.notify.assert_not_called()
        self.assertIsNone(self.store.get_message(result.message_id, include_body=True)["body"])
        self.assertNotIn("text", self.store.get_message(result.message_id))
        self.assertEqual(self.records()[0]["kind"], "new")
        self.assertNotIn("private fixture text", (self.root / "inbox/support.jsonl").read_text())

    def test_reply_inherits_root_and_followup_is_durable_across_pipeline_restart(self):
        root = self.pipeline.handle_event(event())
        self.now += 20
        self.pipeline = self.make_pipeline()
        reply = self.pipeline.handle_event(event(2, text="untagged reply",
                                                 thread_ts="1000.000001", is_thread_reply=True))
        self.assertEqual(reply.lane, "support")
        self.assertEqual(self.store.get_message(reply.message_id)["route_reason"], "thread_inherit")
        self.assertTrue(self.store.get_message(reply.message_id)["followup"])
        self.assertEqual(self.records()[-1]["kind"], "followup")
        self.assertEqual(self.records()[0]["id"], root.message_id)

    def test_followup_window_expiry_and_edit(self):
        first = self.pipeline.handle_event(event())
        self.now += self.config.service.followup_window_seconds + 1
        reply = self.pipeline.handle_event(event(2, text="plain", thread_ts="1000.000001",
                                                 is_thread_reply=True))
        self.assertFalse(self.store.get_message(reply.message_id)["followup"])
        self.pipeline.handle_event(event(3, ts="1000.000001", subtype="message_changed"))
        self.assertTrue(self.store.get_message(first.message_id)["followup"])
        self.assertEqual(self.records()[-1]["kind"], "followup")

    def test_unmatched_exports_ids_and_uses_only_human_purpose(self):
        result = self.pipeline.handle_event(event(text="no tag"))
        self.assertEqual(result.state, "human_pending")
        self.lane.notify.assert_not_called()
        self.human.notify.assert_called_once_with("human_queue", [result.message_id])
        rows = (self.root / "state/human_queue.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(rows[0]), {"id": result.message_id, "suggested_lane": None})

    def test_missing_human_adapter_logs_once(self):
        self.config = replace(self.config, notify_adapters=self.config.notify_adapters[:2])
        self.notifiers = self.notifiers[:2]
        self.pipeline = self.make_pipeline()
        with self.assertLogs("slack_dispatch.pipeline", level="WARNING") as logs:
            self.pipeline.handle_event(event(text="plain"))
            self.pipeline.handle_event(event(2, text="plain"))
        self.assertEqual(len(logs.output), 1)

    def test_duplicate_reaction_meta_and_missing_edit_do_not_route(self):
        original = event()
        result = self.pipeline.handle_event(original)
        self.assertTrue(self.pipeline.handle_event(original).duplicate)
        self.assertTrue(self.pipeline.handle_event(dict(original, event_id="EvDUMMY9")).duplicate)
        reaction = self.pipeline.handle_event(event(2, ts=original["ts"], event_type="reaction_added",
                                                    reaction="eyes"))
        self.assertEqual(reaction.kind, "reaction_added")
        self.assertEqual(len(self.store.list_reactions(result.message_id)), 1)
        meta = self.pipeline.handle_event(event(3, event_type="channel_created"))
        self.assertEqual(meta.kind, "channel_meta")
        self.pipeline.handle_event(event(4, subtype="message_changed"))
        self.assertEqual(len(self.store.list_messages()), 1)
        self.lane.notify.assert_called_once()

    def test_excluded_owner_mention_and_deletion(self):
        excluded = self.pipeline.handle_event(event(channel="C0000000003"))
        self.assertEqual(excluded.state, "excluded")
        human = self.pipeline.handle_event(event(2, text="[support] <@U0000000093>"))
        self.assertEqual(human.state, "human_pending")
        self.pipeline.handle_event(event(3, ts="1000.000002", subtype="message_deleted"))
        self.assertEqual(self.store.get_message(human.message_id)["note"], "message_deleted")
        self.human.notify.assert_called_once()
        self.lane.notify.assert_not_called()

    def test_debounce_flush_does_not_duplicate_durable_inbox(self):
        self.pipeline.handle_event(event())
        self.now += 1
        second = self.pipeline.handle_event(event(2))
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(self.lane.notify.call_count, 1)
        self.now = 1090
        self.pipeline.flush(self.now)
        self.lane.notify.assert_called_with("support", [second.message_id])
        self.assertEqual(len(self.records()), 2)
        self.assertNotIn("private", repr(self.pipeline.wake_engine.pending))

    def test_wake_disabled_keeps_inbox(self):
        (self.root / "state").mkdir()
        (self.root / "state/wake.off").touch()
        self.pipeline.handle_event(event())
        self.pipeline.flush(2000)
        self.assertEqual(len(self.records()), 1)
        self.lane.notify.assert_not_called()

    def test_storm_batches_ids_and_notifies_human(self):
        self.config = replace(self.config, wake_policy=replace(self.config.wake_policy,
                                                               storm_per_hour=2, batch_seconds=10))
        self.pipeline = self.make_pipeline()
        self.pipeline.handle_event(event())
        second = self.pipeline.handle_event(event(2))
        self.human.notify.assert_called_once_with("human_queue", [second.message_id])
        self.now += 10
        self.pipeline.flush(self.now)
        self.lane.notify.assert_called_with("support", [second.message_id])

    def test_failed_adapter_retries_without_repeating_successful_inbox(self):
        self.lane.notify.side_effect = [RuntimeError("private fixture text"), None]
        with self.assertLogs("slack_dispatch.pipeline", level="ERROR") as logs:
            self.pipeline.handle_event(event())
        self.assertNotIn("private fixture text", " ".join(logs.output))
        self.pipeline.flush(1100)
        self.assertEqual(self.lane.notify.call_count, 2)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.pipeline.snapshot()["failures"], 1)

    def test_rerouted_or_done_pending_items_are_not_woken(self):
        self.pipeline.handle_event(event())
        second = self.pipeline.handle_event(event(2))
        self.store.set_state(second.message_id, "done")
        self.pipeline.flush(2000)
        self.lane.notify.assert_called_once()

    def test_human_queue_selection_supports_every_adapter_kind(self):
        self.store.upsert_lane("human_queue", session_pointer="dummy-human-session")
        for number, kind in enumerate(("file_inbox", "file_drop", "assistant_dm", "session_message"), 1):
            with self.subTest(kind=kind):
                human_spec = replace(self.config.notify_adapters[-1], kind=kind)
                self.config = replace(self.config, notify_adapters=(human_spec,))
                receiver = Mock()
                self.notifiers = [receiver]
                pipeline = self.make_pipeline()
                result = pipeline.handle_event(event(number, text="unmatched"))
                receiver.notify.assert_called_once_with("human_queue", [result.message_id])

    def test_followup_window_includes_exact_boundary(self):
        self.pipeline.handle_event(event())
        self.now += self.config.service.followup_window_seconds
        result = self.pipeline.handle_event(event(2, text="plain", thread_ts="1000.000001",
                                                  is_thread_reply=True))
        self.assertTrue(self.store.get_message(result.message_id)["followup"])
