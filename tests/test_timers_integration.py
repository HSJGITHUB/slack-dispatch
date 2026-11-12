"""Offline timer thresholds, restart receipts and retry checks."""
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.file_drop import FileDropNotifier
from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.session_message import SessionMessageNotifier
from slack_dispatch.timers import TimerSweep
from tests.test_pipeline_integration import event, fixture_config


class TimerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dispatch-timers-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = fixture_config(self.root)
        self.config = replace(self.config, timers=replace(self.config.timers,
                              unassigned_seconds=10, unacknowledged_seconds=20, reassign_seconds=40),
                              notify_adapters=self.config.notify_adapters[1:])
        self.store = LedgerStore(self.config.ledger.path)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        self.store.upsert_lane("support")
        self.lane, self.human = Mock(), Mock()
        self.now = 1000.0
        self.clock = lambda: self.now
        self.sweep = TimerSweep(self.config, self.store, [self.lane, self.human], self.clock)

    def assigned(self, number=1):
        mid = self.store.record_event(event(number)).message_id
        with patch("slack_dispatch.ledger.store.now", return_value=datetime.fromtimestamp(
                1000, timezone.utc).isoformat()):
            self.store.set_route(mid, "support", "fixture")
        return mid

    def test_each_threshold_fires_once_across_restart_without_mutating_assignment(self):
        assigned = self.assigned()
        unassigned = self.store.record_event(event(2)).message_id
        pending = self.store.record_event(event(3)).message_id
        self.store.set_state(pending, "human_pending", note="preserved note")
        stamp = self.store.get_message(assigned)["assigned_at"]
        self.assertEqual(self.sweep.run_once(1009), 0)
        self.assertEqual(self.sweep.run_once(1010), 2)
        self.assertEqual(self.sweep.run_once(1019), 0)
        self.assertEqual(self.sweep.run_once(1020), 1)
        self.assertEqual(self.sweep.run_once(1039), 0)
        self.assertEqual(self.sweep.run_once(1040), 1)
        self.lane.notify.assert_called_once_with("support", [assigned])
        self.assertEqual(self.human.notify.call_count, 3)
        self.assertEqual({c.args[1][0] for c in self.human.notify.call_args_list},
                         {assigned, unassigned, pending})
        self.store.close()
        self.store.open()
        self.store.migrate()
        restarted = TimerSweep(self.config, self.store, [self.lane, self.human], self.clock)
        self.assertEqual(restarted.run_once(5000), 0)
        self.assertEqual(self.store.get_message(assigned)["assigned_at"], stamp)
        self.assertEqual(self.store.get_message(pending)["note"], "preserved note")

    def test_failures_retry_only_unsent_adapters(self):
        self.config = replace(self.config, notify_adapters=(self.config.notify_adapters[0],) * 2)
        good, flaky = Mock(), Mock()
        flaky.notify.side_effect = [RuntimeError("private payload"), None]
        sweep = TimerSweep(self.config, self.store, [good, flaky], self.clock)
        self.assigned()
        with self.assertLogs("slack_dispatch.timers", level="ERROR") as logs:
            self.assertEqual(sweep.run_once(1020), 1)
        self.assertNotIn("private payload", " ".join(logs.output))
        self.assertEqual(sweep.run_once(1020), 1)
        good.notify.assert_called_once()
        self.assertEqual(flaky.notify.call_count, 2)

    def test_acknowledged_done_and_excluded_are_ignored(self):
        for number, state in enumerate(("acked", "done", "excluded"), 1):
            mid = self.assigned(number)
            self.store.set_state(mid, state)
        self.assertEqual(self.sweep.run_once(10000), 0)
        self.lane.notify.assert_not_called()
        self.human.notify.assert_not_called()

    def test_sweep_is_not_limited_to_first_hundred_items(self):
        for number in range(1, 106):
            self.store.record_event(event(number))
        self.assertEqual(self.sweep.run_once(1010), 105)
        self.assertEqual(self.sweep.run_once(1020), 0)

    def test_dry_run_does_not_consume_receipts(self):
        self.assigned()
        dry = Mock(service_dry_run=True)
        sweep = TimerSweep(self.config, self.store, [dry, dry], self.clock)
        self.assertEqual(sweep.run_once(1040), 0)
        self.assertEqual(self.store._rows("SELECT * FROM timer_events"), [])

    def test_missing_human_adapter_is_not_marked_sent(self):
        mid = self.store.record_event(event()).message_id
        self.config = replace(self.config, notify_adapters=())
        sweep = TimerSweep(self.config, self.store, [], self.clock)
        with self.assertLogs("slack_dispatch.timers", level="WARNING") as logs:
            sweep.run_once(1010)
            sweep.run_once(1020)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(self.store.get_message(mid)["state"], "new")
        self.assertEqual(self.store._rows("SELECT * FROM timer_events"), [])

    def test_blocked_session_notice_keeps_receipt_available(self):
        spec = replace(self.config.notify_adapters[0], kind="session_message")
        # Null config sessions so the ledger pointer (claim) decides, as before P8.
        lanes = tuple(replace(lane, session=None) for lane in self.config.lanes)
        self.config = replace(self.config, notify_adapters=(spec,), lanes=lanes)
        self.assigned()
        sweep = TimerSweep(self.config, self.store, [self.lane], self.clock)
        self.assertEqual(sweep.run_once(1020), 0)
        self.store.upsert_lane("support", session_pointer="dummy-session", paused=True)
        self.assertEqual(sweep.run_once(1020), 0)
        self.store.upsert_lane("support", paused=False)
        state = self.root / "state"
        state.mkdir()
        (state / "wake.off").touch()
        self.assertEqual(sweep.run_once(1020), 0)
        (state / "wake.off").rename(state / "wake.off.saved")
        self.assertEqual(sweep.run_once(1020), 1)
        self.assertEqual(sweep.run_once(1020), 0)
        self.lane.notify.assert_called_once()

    def test_reminders_and_reassignment_leave_file_inbox_records_unchanged(self):
        base = fixture_config(self.root).notify_adapters[0]
        config = replace(self.config, notify_adapters=(
            base, self.config.notify_adapters[0],
            replace(base, purpose="human_queue"), self.config.notify_adapters[1]))
        inbox = FileInboxNotifier(config.inbox.root, self.store.get_message)
        mid = self.assigned()
        for lane in ("support", "human_queue"):
            inbox.notify(lane, [mid])
        paths = [self.root / "inbox" / (lane + ".jsonl")
                 for lane in ("support", "human_queue")]
        before = [path.read_bytes() for path in paths]
        sweep = TimerSweep(config, self.store, [inbox, self.lane, inbox, self.human], self.clock)
        self.assertEqual(sweep.run_once(1019), 0)
        self.assertEqual(sweep.run_once(1020), 1)
        self.assertEqual(sweep.run_once(1040), 1)
        self.assertEqual(sweep.run_once(1100), 0)
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertEqual([len(path.read_text().splitlines()) for path in paths], [1, 1])
        self.lane.notify.assert_called_once_with("support", [mid])
        self.human.notify.assert_called_once_with("human_queue", [mid])
        self.assertEqual({row["adapter"] for row in
                          self.store._rows("SELECT adapter FROM timer_events")}, {1, 3})

    def test_unassigned_human_notice_still_supports_file_inbox(self):
        base = fixture_config(self.root).notify_adapters[0]
        config = replace(self.config, notify_adapters=(replace(base, purpose="human_queue"),))
        inbox = FileInboxNotifier(config.inbox.root, self.store.get_message)
        mid = self.store.record_event(event()).message_id
        sweep = TimerSweep(config, self.store, [inbox], self.clock)
        self.assertEqual(sweep.run_once(1010), 1)
        self.assertEqual(sweep.run_once(1040), 0)
        records = (self.root / "inbox/human_queue.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(record)["id"] for record in records], [mid])

    def test_session_reminder_text_preserves_target_ids_and_shared_template(self):
        self.store.upsert_lane("support", session_pointer="dummy-session")
        self.store.upsert_lane("human_queue", session_pointer="dummy-human-session")
        specs = tuple(replace(spec, kind="session_message") for spec in self.config.notify_adapters)
        config = replace(self.config, notify_adapters=specs)
        for number, template in enumerate((
                ("dummy-command", "{target}", "{message}"),
                'dummy-command "{target}" "{message}"'), 1):
            with self.subTest(template=template):
                runner = Mock(return_value=Mock(returncode=0))
                notifier = SessionMessageNotifier(template, lambda lane: "dummy-" + lane,
                                                  runner=runner)
                mid = self.assigned(number)
                sweep = TimerSweep(config, self.store, [notifier, notifier], self.clock)
                self.assertEqual(sweep.run_once(1040), 2)
                commands = [call.args[0] for call in runner.call_args_list]
                self.assertEqual([command[1] for command in commands],
                                 ["dummy-support", "dummy-human_queue"])
                for command in commands:
                    self.assertIn("reminder", command[2])
                    self.assertIn(mid, command[2])
                    self.assertNotIn("private fixture text", command[2])
                self.assertIn("reassignment proposal", commands[1][2])
                self.assertEqual(notifier.command_template, template)
                notifier.notify("support", [mid])
                self.assertNotIn("reminder", runner.call_args.args[0][2])

    def test_file_drop_reminder_notice_preserves_shared_template_and_ids(self):
        template = {"lane": "{lane}", "ids": "{ids}", "count": "{count}"}
        for number, kind in enumerate(("file_drop", "assistant_dm"), 1):
            with self.subTest(kind=kind):
                config = replace(self.config, notify_adapters=tuple(
                    replace(spec, kind=kind) for spec in self.config.notify_adapters))
                directory = self.root / kind
                notifier = FileDropNotifier(directory, template)
                mid = self.assigned(number)
                sweep = TimerSweep(config, self.store, [notifier, notifier], self.clock)
                self.assertEqual(sweep.run_once(1040), 2)
                records = [json.loads(path.read_text(encoding="utf-8"))
                           for path in directory.glob("*.json")]
                self.assertEqual(len(records), 2)
                self.assertEqual({row["lane"] for row in records}, {"support", "human_queue"})
                for row in records:
                    self.assertIn("reminder", row["notice"])
                    self.assertEqual(row["ids"], [mid])
                    self.assertEqual(row["count"], 1)
                self.assertNotIn("notice", notifier.template)
                self.assertEqual(template, {"lane": "{lane}", "ids": "{ids}", "count": "{count}"})

    def test_each_relevant_state_is_queried_once_and_handles_over_hundred_rows(self):
        for number in range(1, 106):
            self.assigned(number)
            self.store.record_event(event(number + 105))
            mid = self.store.record_event(event(number + 210)).message_id
            self.store.set_state(mid, "human_pending")
            ignored = self.store.record_event(event(number + 315)).message_id
            self.store.set_state(ignored, "done")
        with patch.object(self.store, "list_messages", wraps=self.store.list_messages) as query:
            self.assertEqual(self.sweep.run_once(1040), 420)
        self.assertEqual(query.call_count, 3)
        self.assertEqual([call.kwargs["state"] for call in query.call_args_list],
                         ["assigned", "new", "human_pending"])
        self.assertEqual(self.sweep.run_once(1040), 0)
