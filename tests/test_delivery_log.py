"""P18 delivery log: relay capture, one row per attempt, receipts, CLI, pruning (dummy ids only)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from slack_dispatch.config.loader import ConfigError, parse_config
from slack_dispatch.config.schema import DeliveryLog, Lane, NotifyAdapter
from slack_dispatch.console import queries
from slack_dispatch.inbox import cli
from slack_dispatch.ledger.store import LedgerStore, utc_stamp
from slack_dispatch.notify import build_notifiers
from slack_dispatch.notify.base import DeliveryRecorder
from slack_dispatch.notify.session_message import classify_relay, relay_fields
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline
from slack_dispatch.router.classifier import NullSuggester
from slack_dispatch.timers import TimerSweep
from tests.test_console_core import sample_data
from tests.test_pipeline_integration import event, fixture_config

ID_A = "0000aaaa-0000-4000-8000-000000000001"
ID_B = "0000aaaa-0000-4000-8000-000000000002"
RELAY_ID = "9999eeee-0000-4000-8000-000000000009"
SENTINEL = "SENTINEL_BODY_P18_7f3a"
DELIVERED = DeliveryLog.delivered_patterns
NOT_DELIVERED = DeliveryLog.not_delivered_patterns
# A standard-form success text: it names the conditional "or refuses it", which is no refusal.
SUCCESS_TEXT = ("Message sent. It is in that session's inbox and is read on its next turn; "
                "the session may ignore or refuses it.")
TOOL_RESULT = '{"success":true,"message":"Message sent to dummy-support"}'
ENOINBOX = '{"success":false,"error":"ENOINBOX: dummy-support has no inbox"}'


def relay_stdout(result, *, is_error=False, cost=0.0123, duration=4200):
    return json.dumps({"type": "result", "subtype": "success", "is_error": is_error,
                       "result": result, "total_cost_usd": cost, "duration_ms": duration,
                       "session_id": RELAY_ID}).encode("utf-8")


def completed(stdout):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr=b"")


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class RelayClassificationTests(unittest.TestCase):
    def test_old_done_form_is_sent_with_unknown_outcome(self):
        fields = relay_fields(relay_stdout("DONE"), ["dummy"], DELIVERED, NOT_DELIVERED)
        self.assertEqual(fields, {"status": "sent", "detail": "unknown | DONE", "cost_usd": 0.0123,
                                  "duration_ms": 4200, "relay_session_id": RELAY_ID})

    def test_echoed_tool_result_is_delivered(self):
        fields = relay_fields(relay_stdout(TOOL_RESULT), ["dummy"], DELIVERED, NOT_DELIVERED)
        self.assertEqual(fields["status"], "sent")
        self.assertEqual(fields["detail"], "delivered | " + TOOL_RESULT)
        spaced = relay_fields(relay_stdout('Result: {"success": true}'), [], DELIVERED, NOT_DELIVERED)
        self.assertTrue(spaced["detail"].startswith("delivered | "))

    def test_conditional_refusal_phrase_is_not_a_refusal(self):
        self.assertEqual(classify_relay(SUCCESS_TEXT, DELIVERED, NOT_DELIVERED), "delivered")
        # Even a configured refusal pattern loses to the earlier success phrase.
        self.assertEqual(classify_relay(SUCCESS_TEXT, DELIVERED, NOT_DELIVERED + ("refuses it",)),
                         "delivered")
        self.assertEqual(classify_relay("The session refuses it.", DELIVERED, NOT_DELIVERED), "unknown")

    def test_enoinbox_and_other_not_delivered_texts(self):
        self.assertEqual(relay_fields(relay_stdout(ENOINBOX), [], DELIVERED, NOT_DELIVERED)["detail"],
                         "not_delivered | " + ENOINBOX)
        for text in ("Session not found", "The message could not be delivered", "enoinbox"):
            self.assertEqual(classify_relay(text, DELIVERED, NOT_DELIVERED), "not_delivered")
        self.assertEqual(classify_relay("", DELIVERED, NOT_DELIVERED), "unknown")

    def test_invalid_json_is_sent_and_unparsed(self):
        fields = relay_fields(b"Traceback: dummy relay crash " + b"x" * 400, [], DELIVERED, NOT_DELIVERED)
        self.assertEqual(fields["status"], "sent")
        self.assertTrue(fields["detail"].startswith("unparsed output | Traceback: dummy relay crash"))
        self.assertLessEqual(len(fields["detail"]), len("unparsed output | ") + 200)
        self.assertEqual(relay_fields(None, [])["detail"], "unparsed output")

    def test_is_error_marks_failed_and_list_output_uses_result(self):
        self.assertEqual(relay_fields(relay_stdout("boom", is_error=True), [])["status"], "failed")
        listed = json.dumps([{"type": "system"}, {"type": "result", "result": TOOL_RESULT}])
        self.assertTrue(relay_fields(listed, [], DELIVERED, NOT_DELIVERED)["detail"].startswith("delivered"))

    def test_echoed_wake_text_is_scrubbed(self):
        message = "Lane support: 1 new inbox item(s) (ids: slack:C0000000002:1000.000001)"
        fields = relay_fields(relay_stdout("Relayed: " + message), ["dummy", message])
        self.assertEqual(fields["detail"], "unknown | Relayed: [message]")


class TempLedger(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p18-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def open_store(self, config):
        store = LedgerStore(config.ledger.path, store_body=config.ledger.store_body)
        store.open()
        store.migrate()
        self.addCleanup(store.close)
        for lane in config.lanes:
            store.upsert_lane(lane.id)
        return store

    def rows(self):
        return list(reversed(self.store.list_deliveries()))


class PipelineDeliveryLogTests(TempLedger):
    """One row per attempt to reach a lane session or the assistant DM."""

    def build(self, session="0000aaaa", sessions=((ID_A, "dummy-support"),), dry_run=False,
              store_body=False, stdout=None, human=None, delivery_log=None, name="one"):
        # Each name is a fresh instance directory (ledger, inbox, sessions).
        self.base = self.root / name
        self.base.mkdir()
        base = fixture_config(self.base)
        directory = self.base / "sessions"
        directory.mkdir()
        for number, (session_id, name) in enumerate(sessions):
            (directory / f"s{number}.json").write_text(
                json.dumps({"sessionId": session_id, "name": name}), encoding="utf-8")
        lane_adapter = NotifyAdapter("session_message", None,
                                     ("dummy-relay", "--target", "{target}", "--message", "{message}"),
                                     dry_run=dry_run, timeout=5, template={}, purpose="lane")
        self.config = replace(
            base, lanes=(Lane("owner", None, False), Lane("support", session, False),
                         Lane("content", None, True)),
            sessions_dir=str(directory), ledger=replace(base.ledger, store_body=store_body),
            notify_adapters=(base.notify_adapters[0], lane_adapter, human or base.notify_adapters[2]),
            delivery_log=delivery_log or DeliveryLog())
        self.store = self.open_store(self.config)
        self.notifiers = build_notifiers(self.config, self.store)
        self.runner = Mock(return_value=completed(stdout if stdout is not None else relay_stdout(TOOL_RESULT)))
        self.notifiers[1].runner = self.runner
        self.clock = Clock()
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(), self.notifiers,
                                 WakePolicyEngine(self.config.wake_policy, self.clock), self.clock)

    def test_sent_wake_stores_relay_result_and_target(self):
        self.build()
        result = self.pipeline.handle_event(event())
        (row,) = self.rows()
        self.assertEqual((row["purpose"], row["kind"], row["lane"], row["adapter"], row["status"]),
                         ("lane", "new", "support", "session_message", "sent"))
        self.assertEqual((row["target_session_id"], row["target_session_name"]), (ID_A, "dummy-support"))
        self.assertEqual(json.loads(row["item_ids"]), [result.message_id])
        self.assertEqual(row["item_count"], 1)
        self.assertEqual(row["detail"], "delivered | " + TOOL_RESULT)
        self.assertEqual((row["cost_usd"], row["duration_ms"], row["relay_session_id"]),
                         (0.0123, 4200, RELAY_ID))
        self.assertEqual(row["created_at"], utc_stamp(1000.0))

    def test_old_done_output_still_works(self):
        self.build(stdout=relay_stdout("DONE"))
        self.pipeline.handle_event(event())
        self.assertEqual([(row["status"], row["detail"]) for row in self.rows()], [("sent", "unknown | DONE")])

    def test_enoinbox_is_sent_but_not_delivered(self):
        self.build(stdout=relay_stdout(ENOINBOX))
        self.pipeline.handle_event(event())
        self.assertEqual(self.rows()[0]["detail"], "not_delivered | " + ENOINBOX)
        self.assertEqual(queries.deliveries_summary(self.store, 1000.0)["not_delivered"], 1)

    def test_invalid_json_output(self):
        self.build(stdout=b"plain text answer")
        self.pipeline.handle_event(event())
        self.assertEqual((self.rows()[0]["status"], self.rows()[0]["detail"]),
                         ("sent", "unparsed output | plain text answer"))

    def test_timeout_writes_one_timeout_row_per_attempt(self):
        self.build()
        self.runner.side_effect = subprocess.TimeoutExpired("dummy-relay", 5)
        with self.assertLogs("slack_dispatch.pipeline", level="ERROR"):
            self.pipeline.handle_event(event())
        self.assertEqual(self.runner.call_count, 2)  # the adapter's own retry is one attempt
        (row,) = self.rows()
        self.assertEqual((row["status"], row["detail"]), ("timeout", "TimeoutExpired: no answer within 5s"))
        self.assertEqual(row["target_session_name"], "dummy-support")
        # The pipeline retries failed wakes on flush: a second attempt, a second row.
        self.runner.side_effect = None
        self.pipeline.flush(1001.0)
        self.assertEqual([row["status"] for row in self.rows()], ["timeout", "sent"])

    def test_failed_relay_detail_has_no_command_text(self):
        self.build(store_body=True)
        self.runner.side_effect = subprocess.CalledProcessError(3, ["dummy-relay", SENTINEL])
        with self.assertLogs("slack_dispatch.pipeline", level="ERROR"):
            self.pipeline.handle_event(event(text="[support] " + SENTINEL))
        (row,) = self.rows()
        self.assertEqual((row["status"], row["detail"]), ("failed", "CalledProcessError: exit status 3"))

    def test_skipped_no_session_ambiguous_and_paused(self):
        self.build(session="2222cccc")
        self.pipeline.handle_event(event())
        self.assertEqual(self.rows()[-1]["status"], "skipped_no_session")
        self.runner.assert_not_called()

        self.build(sessions=((ID_A, "dummy-a"), (ID_B, "dummy-b")), name="two")
        with self.assertLogs("slack_dispatch.notify.session_resolve", level="WARNING"):
            self.pipeline.handle_event(event())
        self.assertEqual(self.rows()[-1]["status"], "skipped_ambiguous")
        self.assertEqual(self.rows()[-1]["kind"], "new")

        self.build(name="three")
        self.store.upsert_lane("support", paused=True)
        self.pipeline.handle_event(event())
        self.assertEqual(self.rows()[-1]["status"], "skipped_paused")
        self.runner.assert_not_called()
        self.assertEqual(len(self.rows()), 1)  # one row per attempt

    def test_adapter_dry_run_and_service_dry_run(self):
        self.build(dry_run=True)
        self.pipeline.handle_event(event())
        (row,) = self.rows()
        self.assertEqual((row["status"], row["target_session_name"]), ("dry_run", "dummy-support"))
        self.runner.assert_not_called()

        class DryRun:
            service_dry_run = True

            def notify(self, lane, message_ids):
                return None

        self.build(name="two")
        self.notifiers[1] = DryRun()
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(), self.notifiers,
                                 WakePolicyEngine(self.config.wake_policy, self.clock), self.clock)
        self.pipeline.handle_event(event())
        self.assertEqual([row["status"] for row in self.rows()], ["dry_run"])

    def test_file_inbox_is_never_logged_and_disabled_log_writes_nothing(self):
        self.build(delivery_log=DeliveryLog(enabled=False))
        self.pipeline.handle_event(event())
        self.runner.assert_called_once()
        self.assertEqual(self.rows(), [])
        self.assertTrue((self.base / "inbox/support.jsonl").exists())

    def test_human_digest_row_covers_every_id(self):
        self.build()
        self.config = replace(self.config, human_queue=replace(self.config.human_queue, mode="digest"))
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(), self.notifiers,
                                 WakePolicyEngine(self.config.wake_policy, self.clock), self.clock)
        ids = [self.pipeline.handle_event(event(number, text="no tag", user=f"U00000001{number:02d}")).message_id
               for number in range(1, 24)]
        self.pipeline.flush(1000.0 + self.config.human_queue.digest_seconds)
        (row,) = [row for row in self.rows() if row["purpose"] == "human_queue"]
        self.assertEqual((row["kind"], row["lane"], row["adapter"], row["status"]),
                         ("digest", "human_queue", "file_drop", "sent"))
        self.assertEqual(sorted(json.loads(row["item_ids"])), sorted(ids))
        self.assertEqual(row["item_count"], 23)

    def test_attention_notice_through_session_adapter_and_human_dry_run(self):
        human = NotifyAdapter("session_message", None, ("dummy-relay", "--target", "{target}", "--message",
                                                        "{message}"), timeout=5,
                              template={"target": "dummy-operator"}, purpose="human_queue")
        self.build(human=human, sessions=((ID_A, "dummy-support"), (ID_B, "dummy-operator")))
        self.notifiers[2].runner = self.runner
        owner = self.config.always_human.owner_user
        self.pipeline.handle_event(event(text=f"no tag <@{owner}>"))
        (row,) = [row for row in self.rows() if row["purpose"] == "human_queue"]
        self.assertEqual((row["kind"], row["status"], row["target_session_name"], row["target_session_id"]),
                         ("attention", "sent", "dummy-operator", ID_B))

        self.build(human=replace(human, dry_run=True), name="two")
        self.pipeline.handle_event(event(text=f"no tag <@{owner}>"))
        (row,) = [row for row in self.rows() if row["purpose"] == "human_queue"]
        self.assertEqual((row["kind"], row["status"]), ("attention", "dry_run"))

    def test_card_row_never_holds_the_body(self):
        card = NotifyAdapter("dispatch_card", str(self.root / "cards"), purpose="human_queue")
        self.build(human=card, store_body=True)
        owner = self.config.always_human.owner_user
        result = self.pipeline.handle_event(event(text=f"no tag <@{owner}> {SENTINEL}"))
        self.assertEqual(result.state, "human_pending")
        (row,) = [row for row in self.rows() if row["purpose"] == "human_queue"]
        self.assertEqual((row["kind"], row["adapter"], row["status"], row["detail"]),
                         ("card", "dispatch_card", "sent", "cards written 1"))
        self.assertIn(SENTINEL, next((self.root / "cards/cards").glob("*.json")).read_text(encoding="utf-8"))
        self.assertNotIn(SENTINEL, json.dumps(self.store.list_deliveries()))

    def test_detail_never_contains_the_body(self):
        message = "{message}"
        self.build(store_body=True)
        self.runner.side_effect = lambda command, **kwargs: completed(
            relay_stdout("Relayed verbatim: " + command[4]))
        result = self.pipeline.handle_event(event(text="[support] " + SENTINEL))
        self.assertIn(SENTINEL, self.store.get_message(result.message_id, include_body=True)["body"])
        dumped = json.dumps(self.store.list_deliveries())
        self.assertNotIn(SENTINEL, dumped)
        self.assertNotIn(message, dumped)
        self.assertEqual(self.rows()[0]["detail"], "unknown | Relayed verbatim: [message]")

    def test_reroute_kind_and_timer_reminder_kind(self):
        self.build()
        mid = self.store.record_event(event()).message_id
        self.store.set_route(mid, "support", "reroute:content")
        self.pipeline._send("lane", "support", [mid], inbox=False)
        self.assertEqual(self.rows()[-1]["kind"], "reroute")
        sweep = TimerSweep(self.config, self.store, self.notifiers, self.clock, human=self.pipeline.human)
        self.store.set_route(mid, "support", "explicit_tag")
        later = datetime.now(timezone.utc).timestamp() + self.config.timers.unacknowledged_seconds + 60
        sweep.run_once(later)
        reminders = [row for row in self.rows() if row["kind"] == "reminder"]
        self.assertEqual([(row["lane"], row["status"]) for row in reminders], [("support", "sent")])

    def test_recorder_write_failure_never_breaks_delivery(self):
        self.build()
        with patch.object(self.store, "add_delivery", side_effect=sqlite3.OperationalError("locked")), \
                self.assertLogs("slack_dispatch.notify.base", level="ERROR") as logs:
            result = self.pipeline.handle_event(event())
        self.assertEqual(result.state, "assigned")
        self.runner.assert_called_once()
        self.assertEqual(logs.output, ["ERROR:slack_dispatch.notify.base:Delivery log write failed "
                                       "class=OperationalError"])


    # fix1: relay start stamp, skipped repeat folding, bookkeeping never blocks a delivery.

    def test_ack_during_the_relay_counts_as_receipt(self):
        self.build()
        self.clock.now = datetime(2026, 11, 13, 1, 0, tzinfo=timezone.utc).timestamp()
        start = self.clock.now
        acked = {}

        def slow_relay(command, **kwargs):
            # The lane acks 10 s into a relay that takes 30 s.
            mid = acked["id"] = self.store.list_messages(lane="support")[0]["id"]
            self.store._update(mid, {"ack_at": datetime.fromtimestamp(start + 10, timezone.utc).isoformat()})
            self.clock.now = start + 30
            return completed(relay_stdout(TOOL_RESULT))

        self.runner.side_effect = slow_relay
        self.pipeline.handle_event(event())
        (row,) = self.rows()
        self.assertEqual(row["created_at"], utc_stamp(start))
        receipt = self.store.delivery_receipts([row], start + 60)[0]
        self.assertEqual((receipt["latency_seconds"], receipt["age_seconds"]), (10, None))
        self.assertIsNotNone(acked["id"])

    def test_repeated_skips_on_flush_retries_fold_into_one_row(self):
        self.build()
        self.runner.side_effect = OSError(2, "dummy")
        with self.assertLogs("slack_dispatch.pipeline", level="ERROR"):
            mid = self.pipeline.handle_event(event()).message_id
        self.store.upsert_lane("support", paused=True)
        for now in (1030.0, 1060.0, 1090.0):
            self.clock.now = now  # the service passes its clock to flush
            self.pipeline.flush(now)
        rows = self.rows()
        self.assertEqual([row["status"] for row in rows], ["failed", "skipped_paused"])
        self.assertEqual((rows[1]["repeat_count"], rows[1]["created_at"], rows[1]["last_seen_at"]),
                         (3, utc_stamp(1030.0), utc_stamp(1090.0)))
        shaped = queries.delivery_row(self.store, queries.View(self.config), rows[1], {})
        self.assertEqual(shaped["repeat_count"], 3)
        self.assertEqual(cli.delivery_line(dict(shaped, receipt=dict(
            shaped["receipt"], received_time=None))).split(" ")[-2:], ["x3", "-"])
        # Another status in between starts a new row.
        self.store.upsert_lane("support", paused=False)
        self.runner.side_effect = None
        self.clock.now = 1120.0
        self.pipeline.flush(1120.0)
        self.store.set_route(mid, "support", "explicit_tag")
        self.pipeline._retry[(1, "support")] = {mid}
        self.store.upsert_lane("support", paused=True)
        self.clock.now = 1150.0
        self.pipeline.flush(1150.0)
        self.assertEqual([(row["status"], row["repeat_count"]) for row in self.rows()],
                         [("failed", None), ("skipped_paused", 3), ("sent", None),
                          ("skipped_paused", None)])
        self.assertEqual(queries.delivery_row(self.store, queries.View(self.config),
                                              self.rows()[-1], {})["repeat_count"], 1)

    def test_bookkeeping_errors_never_block_the_wake(self):
        self.build()
        with patch("slack_dispatch.pipeline.lane_kind", side_effect=RuntimeError("dummy")), \
                self.assertLogs("slack_dispatch.pipeline", level="ERROR"):
            result = self.pipeline.handle_event(event())
        self.assertEqual(result.state, "assigned")
        self.runner.assert_called_once()
        self.assertEqual(self.pipeline.snapshot()["failures"], 0)
        self.assertEqual([(row["status"], row["kind"]) for row in self.rows()], [("sent", "new")])

        self.build(name="two")
        self.store.upsert_lane("support", paused=True)
        with patch("slack_dispatch.pipeline.skip_reason", side_effect=RuntimeError("dummy")), \
                self.assertLogs("slack_dispatch.pipeline", level="ERROR"):
            result = self.pipeline.handle_event(event())
        self.assertEqual(result.state, "assigned")
        self.assertEqual(self.pipeline.snapshot()["failures"], 0)
        self.assertEqual(self.rows(), [])

    def test_unreadable_relay_output_keeps_the_delivery_successful(self):
        self.build()
        with patch("slack_dispatch.notify.session_message.relay_fields",
                   side_effect=ValueError("dummy")):
            result = self.pipeline.handle_event(event())
        self.runner.assert_called_once()
        self.assertEqual(self.pipeline.snapshot()["failures"], 0)
        self.assertEqual(self.pipeline.snapshot()["delivered"], 2)  # inbox and wake groups
        self.assertEqual(self.pipeline._retry, {})
        (row,) = self.rows()
        self.assertEqual((row["status"], row["detail"]), ("sent", "relay output not read | ValueError"))
        self.assertEqual(json.loads(row["item_ids"]), [result.message_id])


class ReceiptTests(TempLedger):
    def setUp(self):
        super().setUp()
        config = fixture_config(self.root)
        self.store = self.open_store(config)
        self.wake = datetime(2026, 11, 13, 1, 0, tzinfo=timezone.utc)
        self.ids = [self.store.record_event(event(number)).message_id for number in (1, 2, 3)]
        for mid in self.ids:
            self.store.set_route(mid, "support", "explicit_tag")

    def at(self, minutes):
        return (self.wake + timedelta(minutes=minutes)).isoformat()

    def stamp(self, mid, **fields):
        self.store._update(mid, fields)

    def add(self, ids, *, purpose="lane", kind="new", lane="support", status="sent"):
        return self.store.add_delivery({"created_at": self.wake.isoformat(timespec="microseconds"),
                                        "purpose": purpose, "kind": kind, "lane": lane,
                                        "adapter": "session_message", "item_ids": ids, "status": status})

    def receipt(self, ids, now_minutes=30, **row):
        self.add(ids, **row)
        rows = self.store.list_deliveries()
        return self.store.delivery_receipts(rows[:1], (self.wake + timedelta(minutes=now_minutes)).timestamp())[0]

    def test_earliest_ack_or_done_after_the_wake_wins(self):
        self.stamp(self.ids[0], ack_at=self.at(9))
        self.stamp(self.ids[1], done_at=self.at(4))
        self.stamp(self.ids[2], ack_at=self.at(6))
        receipt = self.receipt(self.ids)
        self.assertEqual((receipt["received_at"], receipt["latency_seconds"], receipt["age_seconds"]),
                         (self.at(4), 240, None))

    def test_ack_before_the_wake_is_ignored(self):
        self.stamp(self.ids[0], ack_at=self.at(-5), done_at=self.at(7))
        receipt = self.receipt(self.ids[:1])
        self.assertEqual((receipt["received_at"], receipt["latency_seconds"]), (self.at(7), 420))

    def test_ack_by_another_lane_is_ignored(self):
        self.stamp(self.ids[0], ack_at=self.at(3))
        self.store.set_route(self.ids[0], "content", "reroute:support")  # rerouted later
        receipt = self.receipt(self.ids[:1])
        self.assertEqual((receipt["received_at"], receipt["age_seconds"]), (None, 1800))

    def test_no_ack_gives_age_and_system_done_is_no_receipt(self):
        self.stamp(self.ids[0], done_at=self.at(2), note="owner_reaction:white_check_mark")
        receipt = self.receipt(self.ids[:1], now_minutes=45)
        self.assertEqual((receipt["received_at"], receipt["latency_seconds"], receipt["age_seconds"]),
                         (None, None, 2700))

    def test_human_queue_rows_have_no_receipt_and_cards_show_decisions(self):
        receipt = self.receipt(self.ids[:1], purpose="human_queue", kind="digest", lane="human_queue")
        self.assertEqual((receipt["received_at"], receipt["age_seconds"], receipt["decision_at"]),
                         (None, None, None))
        self.stamp(self.ids[1], route_reason="human:card", assigned_at=self.at(12))
        card = self.receipt(self.ids[1:2], purpose="human_queue", kind="card", lane="human_queue")
        self.assertEqual(card["decision_at"], self.at(12))

    def test_summary_counts(self):
        self.stamp(self.ids[0], ack_at=self.at(1))
        self.add(self.ids[:1])
        self.add(self.ids[1:2])
        self.add(self.ids[2:], status="skipped_no_session")
        self.add(self.ids[2:], status="skipped_ambiguous")
        self.add(self.ids[2:], status="failed")
        self.add(self.ids[2:], status="timeout")
        self.add(self.ids[2:], status="dry_run")
        self.add(self.ids, purpose="human_queue", kind="digest", lane="human_queue")
        summary = queries.deliveries_summary(self.store, (self.wake + timedelta(minutes=5)).timestamp())
        self.assertEqual(summary, {"hours": 48, "total": 8, "sent": 3, "failed": 2, "timeout": 1,
                                   "skipped": 2, "dry_run": 1, "not_delivered": 0, "unconfirmed": 1})
        later = (self.wake + timedelta(hours=49)).timestamp()
        self.assertEqual(queries.deliveries_summary(self.store, later)["total"], 0)
        self.assertEqual(queries.deliveries_summary(self.store, later, hours=0)["total"], 8)

    def test_filters_and_paging(self):
        for status in ("sent", "failed", "skipped_paused"):
            self.add(self.ids[:1], status=status)
        self.add(self.ids[:1], purpose="human_queue", kind="digest", lane="human_queue")
        self.assertEqual(self.store.count_deliveries(status="skipped"), 1)
        self.assertEqual(self.store.count_deliveries(purpose="lane"), 3)
        self.assertEqual(self.store.count_deliveries(lane="human_queue"), 1)
        self.assertEqual([row["id"] for row in self.store.list_deliveries(limit=2, offset=1)], [3, 2])

    def test_detail_is_capped(self):
        self.store.add_delivery({"purpose": "lane", "lane": "support", "item_ids": [],
                                 "status": "sent", "detail": "x" * 900})
        self.assertEqual(len(self.store.list_deliveries()[0]["detail"]), 500)


class PruneAndMigrationTests(TempLedger):
    def test_prune_by_keep_days_at_start_and_daily(self):
        config = replace(fixture_config(self.root), delivery_log=DeliveryLog(keep_days=2))
        self.store = self.open_store(config)
        day = 86400
        now = 10 * day
        for age_days in (5, 3, 1, 0):
            self.store.add_delivery({"created_at": utc_stamp(now - age_days * day), "purpose": "lane",
                                     "lane": "support", "item_ids": [], "status": "sent"})
        clock = Clock(now)
        pipeline = Pipeline(config, self.store, NullSuggester(), [Mock(), Mock(), Mock()],
                            WakePolicyEngine(config.wake_policy, clock), clock)
        self.assertEqual(self.store.count_deliveries(), 2)
        self.store.add_delivery({"created_at": utc_stamp(now - 1.5 * day), "purpose": "lane",
                                 "lane": "support", "item_ids": [], "status": "sent"})
        pipeline.flush(now + 3600)  # not a day yet: no prune
        self.assertEqual(self.store.count_deliveries(), 3)
        pipeline.flush(now + day)  # cutoff now - 1 day: the 1.5 day row goes, the 1 day row stays
        self.assertEqual(self.store.count_deliveries(), 2)
        pipeline.flush(now + 2 * day + 1)
        self.assertEqual(self.store.count_deliveries(), 0)

    def test_migration_is_additive_and_idempotent(self):
        path = self.root / "old.sqlite3"
        store = LedgerStore(path)
        store.open()
        store.migrate()
        store._db.execute("DROP TABLE delivery_log")  # a ledger from before P18
        mid = store.record_event(event()).message_id
        store.close()
        for _ in range(2):
            store = LedgerStore(path)
            store.open()
            store.migrate()
            self.assertTrue(store.has_table("delivery_log"))
            self.assertIsNotNone(store.get_message(mid))
            store.add_delivery({"purpose": "lane", "lane": "support", "item_ids": [mid], "status": "sent"})
            store.close()
        store = LedgerStore(path)
        store.open()
        store.migrate()
        self.addCleanup(store.close)
        self.assertEqual(store.count_deliveries(), 2)
        self.assertEqual(store._rows("SELECT version FROM schema_version"), [{"version": 2}])
        columns = [row["name"] for row in store._rows("PRAGMA table_info(delivery_log)")]
        self.assertEqual(columns, ["id", "created_at", "purpose", "lane", "kind", "adapter",
                                   "target_session_id", "target_session_name", "item_count",
                                   "item_ids", "status", "detail", "cost_usd", "duration_ms",
                                   "relay_session_id", "last_seen_at", "repeat_count"])


    def test_repeat_columns_are_added_to_an_earlier_delivery_log(self):
        path = self.root / "early.sqlite3"
        store = LedgerStore(path)
        store.open()
        store.migrate()
        store._db.execute("DROP TABLE delivery_log")
        store._db.execute("CREATE TABLE delivery_log (id INTEGER PRIMARY KEY, created_at TEXT, "
                          "purpose TEXT, lane TEXT, kind TEXT, adapter TEXT, target_session_id TEXT, "
                          "target_session_name TEXT, item_count INTEGER, item_ids TEXT, status TEXT, "
                          "detail TEXT, cost_usd REAL, duration_ms INTEGER, relay_session_id TEXT)")
        store._db.execute("INSERT INTO delivery_log (created_at, purpose, lane, item_ids, status) "
                          "VALUES ('2026-11-13T00:00:00.000000+00:00', 'lane', 'support', '[]', "
                          "'skipped_paused')")
        store.migrate()
        self.addCleanup(store.close)
        store.add_delivery({"created_at": "2026-11-13T00:01:00.000000+00:00", "purpose": "lane",
                            "lane": "support", "item_ids": [], "status": "skipped_paused"})
        (row,) = store.list_deliveries()
        self.assertEqual((row["repeat_count"], row["last_seen_at"]),
                         (2, "2026-11-13T00:01:00.000000+00:00"))

class ConfigTests(unittest.TestCase):
    def test_defaults_sample_and_custom_patterns(self):
        data = sample_data()
        self.assertEqual(parse_config(data).delivery_log, DeliveryLog())
        data.pop("delivery_log")
        self.assertEqual(parse_config(data).delivery_log, DeliveryLog())
        data["delivery_log"] = {"keep_days": 7, "result_patterns": {"delivered": ["DONE"]}}
        loaded = parse_config(data).delivery_log
        self.assertEqual((loaded.keep_days, loaded.delivered_patterns, loaded.not_delivered_patterns),
                         (7, ("DONE",), DeliveryLog.not_delivered_patterns))

    def test_invalid_values(self):
        for section in ({"enabled": "yes"}, {"keep_days": 0}, {"result_patterns": {"other": []}},
                        {"result_patterns": {"delivered": "DONE"}},
                        {"result_patterns": {"not_delivered": [""]}}, []):
            data = sample_data()
            data["delivery_log"] = section
            with self.assertRaises(ConfigError):
                parse_config(data)


class CliTests(TempLedger):
    def setUp(self):
        super().setUp()
        config = fixture_config(self.root)
        self.store = self.open_store(config)
        self.now = datetime(2026, 11, 13, 1, 0, tzinfo=timezone.utc).timestamp()
        mid = self.store.record_event(event()).message_id
        self.store.set_route(mid, "support", "explicit_tag")
        self.mid = mid
        recorder = DeliveryRecorder(self.store, purpose="lane", adapter="session_message",
                                    clock=lambda: self.now - 1200)
        recorder.record("support", [mid], "sent", kind="new", detail="delivered | ok",
                        target_session_id=ID_A, target_session_name="dummy-support",
                        cost_usd=0.01, duration_ms=3000)
        recorder.record("support", [mid], "skipped_no_session", kind="new")
        self.store._update(mid, {"ack_at": datetime.fromtimestamp(self.now - 900, timezone.utc).isoformat()})
        DeliveryRecorder(self.store, purpose="human_queue", adapter="file_drop",
                         clock=lambda: self.now - 600).record("human_queue", [mid], "sent", kind="digest")

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli, "DELIVERY_CLOCK", lambda: self.now):
            code = cli.main(["deliveries", *args], store=self.store, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_text_lines(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "2026-11-13 00:50:00 digest human_queue file_drop 1 sent -",
            "2026-11-13 00:40:00 new support no-session 1 skipped_no_session -",
            "2026-11-13 00:40:00 new support dummy-support(0000aaaa) 1 sent/delivered received 00:45 (+5m)",
        ])

    def test_filters_json_and_errors(self):
        code, out, _ = self.run_cli("--lane", "support", "--status", "skipped", "--json")
        rows = json.loads(out)
        self.assertEqual((code, [row["status"] for row in rows]), (0, ["skipped_no_session"]))
        self.assertEqual(rows[0]["items"][0]["channel"], {"id": "C0000000002", "name": None})
        self.assertEqual(self.run_cli("--hours", "0", "--status", "failed")[1], "No delivery attempts\n")
        self.assertEqual(self.run_cli("--status", "bogus")[0], 2)
        self.assertEqual(self.run_cli("--hours", "-1")[0], 2)
        self.now += 49 * 3600
        self.assertEqual(self.run_cli()[1], "No delivery attempts\n")
        self.assertEqual(len(self.run_cli("--hours", "0")[1].splitlines()), 3)

    def test_unconfirmed_receipt_text(self):
        self.store._update(self.mid, {"ack_at": None})
        line = self.run_cli("--status", "sent", "--lane", "support")[1].strip()
        self.assertTrue(line.endswith("sent/delivered unconfirmed 20m"), line)


if __name__ == "__main__":
    unittest.main()
