"""Offline regressions for the six shadow-day requirements."""
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from slack_dispatch.config.loader import ConfigError, load_config, parse_config
from slack_dispatch.config.schema import HumanQueue, SourceConfig
from slack_dispatch.inbox.cli import main
from slack_dispatch.ingest.slack_reconcile import SlackReconcileSource
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify import build_notifiers
from slack_dispatch.notify.file_drop import FileDropNotifier
from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline
from slack_dispatch.router.engine import route
from slack_dispatch.service import build_service_sources
from slack_dispatch.timers import TimerSweep
from tests.test_pipeline_integration import event, fixture_config, SAMPLE
from tests.test_router_config import raw_config


class ShadowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-shadow-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = fixture_config(self.root)
        self.config = replace(self.config, human_queue=HumanQueue(digest_seconds=10))
        self.store = LedgerStore(self.config.ledger.path)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id, session_pointer="dummy-session", paused=False)
        self.now = 1000.0
        self.clock = lambda: self.now
        self.receivers = [Mock(), Mock(), Mock()]
        self.suggester = Mock(enabled=True)
        self.suggester.suggest.return_value = None
        self.pipeline = self.make_pipeline()

    def make_pipeline(self):
        return Pipeline(self.config, self.store, self.suggester, self.receivers,
                        WakePolicyEngine(self.config.wake_policy, self.clock), self.clock)

    def drops(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in (self.root / "human").glob("*.json")]

    def use_drop(self):
        self.receivers[-1] = FileDropNotifier(self.root / "human", {"ids": "{ids}", "count": "{count}"})
        self.pipeline = self.make_pipeline()

    def test_digest_boundary_channel_counts_twenty_ids_and_privacy(self):
        self.use_drop()
        for number in range(1, 26):
            self.pipeline.handle_event(event(number, text="private fixture text",
                                              channel="CDUMMY1" if number <= 20 else "CDUMMY2"))
        self.pipeline.flush(1009)
        self.assertEqual(self.drops(), [])
        self.pipeline.flush(1010)
        payload, = self.drops()
        self.assertEqual(payload["count"], 25)
        self.assertEqual(payload["channels"], {"CDUMMY1": 20, "CDUMMY2": 5})
        self.assertEqual(len(payload["ids"]), 20)
        self.assertIn("inbox.cli queue", payload["body"])
        self.assertIn("inbox.cli assign ID LANE", payload["body"])
        self.assertNotIn("private fixture text", json.dumps(payload))
        self.pipeline.flush(9999)
        self.assertEqual(len(self.drops()), 1)
        self.assertEqual(len(self.store._rows("SELECT * FROM human_notices WHERE sent_at IS NOT NULL")), 25)

    def test_file_inbox_human_digest_has_summary_without_changing_lane_records(self):
        self.config = replace(self.config, notify_adapters=(replace(
            self.config.notify_adapters[0], purpose="human_queue"),))
        self.receivers = [FileInboxNotifier(self.root / "inbox", self.store.get_message)]
        self.pipeline = self.make_pipeline()
        result = self.pipeline.handle_event(event(text="plain"))
        self.pipeline.flush(1010)
        path, = (self.root / "inbox/human_queue-notices").glob("*.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["ids"], [result.message_id])
        self.assertEqual(payload["channels"], {"C0000000002": 1})
        self.assertIn("queue", payload["summary"])
        self.assertFalse((self.root / "inbox/human_queue.jsonl").exists())

    def test_next_digest_uses_next_pending_interval(self):
        self.pipeline.handle_event(event(text="plain"))
        self.pipeline.flush(1010)
        self.now = 1011
        self.pipeline.handle_event(event(2, text="plain"))
        self.pipeline.flush(1020)
        self.assertEqual(self.receivers[-1].notify.call_count, 1)
        self.pipeline.flush(1021)
        self.assertEqual(self.receivers[-1].notify.call_count, 2)

    def test_attention_dual_delivery_immediate_fyi_and_one_shot(self):
        self.use_drop()
        result = self.pipeline.handle_event(event(text="[support] <@U0000000093>"))
        self.assertEqual(result.state, "assigned")
        self.assertEqual(self.store.get_message(result.message_id)["attention"], "owner_mention")
        self.receivers[0].notify.assert_called_once_with("support", [result.message_id])
        payload, = self.drops()
        self.assertIn("FYI, assigned to lane support", payload["body"])
        self.assertEqual(payload["ids"], [result.message_id])
        self.pipeline.handle_event(event(2, ts="1000.000001", subtype="message_changed",
                                         text="[support] <@U0000000093>"))
        self.pipeline.flush(2000)
        self.assertEqual(len(self.drops()), 1)
        self.suggester.suggest.assert_not_called()

    def test_attention_without_lane_is_immediate_without_classifier(self):
        result = self.pipeline.handle_event(event(user="U0000000091", text="plain"))
        self.assertEqual(result.state, "human_pending")
        self.receivers[-1].notify.assert_called_once_with("human_queue", [result.message_id])
        self.suggester.suggest.assert_not_called()

    def test_immediate_flags_can_be_disabled_and_excluded_wins(self):
        self.config = replace(self.config, human_queue=replace(self.config.human_queue, immediate_flags=()))
        self.pipeline = self.make_pipeline()
        self.pipeline.handle_event(event(user="U0000000091", text="plain"))
        self.pipeline.handle_event(event(2, user="U0000000091", channel="C0000000003"))
        self.receivers[-1].notify.assert_not_called()
        self.pipeline.flush(1010)
        self.receivers[-1].notify.assert_called_once()
        self.assertEqual(len(self.receivers[-1].notify.call_args.args[1]), 1)

    def test_attention_preserves_remaining_rule_precedence(self):
        cfg = parse_config(raw_config())
        protected = event(user="U0000000091", thread_ts="1", ts="2")
        cases = [({}, "owner", "thread_inherit"),
                 ({}, None, "explicit_tag"),
                 ({"text": "plain", "channel": "C0000000001"}, None, "channel_default"),
                 ({"text": "<@U0000000093>", "user": "U0000000001"}, None, "person_channel")]
        for changes, binding, rule in cases:
            with self.subTest(rule=rule):
                decision = route(dict(protected, **changes), cfg, lambda *args: binding)
                self.assertEqual(decision.rule, rule)
                self.assertIsNotNone(decision.attention)

    def test_bot_default_routes_only_channel_and_never_classifier_or_human(self):
        for number, changes in enumerate(({"bot_id": "BDUMMY"}, {"user": None},
                                         {"is_bot": True}), 1):
            excluded = self.pipeline.handle_event(event(number, text="[support] <@U0000000093>", **changes))
            self.assertEqual(excluded.state, "excluded")
            self.assertEqual(self.store.get_message(excluded.message_id)["note"], "excluded:bot")
            assigned = self.pipeline.handle_event(event(number + 10, channel="C0000000001",
                                                        text="[content] <@U0000000093>", **changes))
            self.assertEqual(assigned.lane, "support")
        self.pipeline.flush(2000)
        self.receivers[-1].notify.assert_not_called()
        self.suggester.suggest.assert_not_called()

    def test_known_bot_policy_alternatives(self):
        cfg = replace(self.config, bot_messages=replace(self.config.bot_messages,
                                                        known_bot_users=("U0000000088",)))
        self.assertEqual(route(event(), cfg, Mock()).reason, "excluded:bot")
        cfg = replace(cfg, bot_messages=replace(cfg.bot_messages, policy="exclude_all"))
        self.assertEqual(route(event(channel="C0000000001"), cfg, Mock()).reason, "excluded:bot")
        cfg = replace(cfg, bot_messages=replace(cfg.bot_messages, policy="route_all"))
        self.assertEqual(route(event(), cfg, Mock()).lane, "support")
        self.assertEqual(route(event(text="plain"), cfg, Mock()).rule, "none")

    def test_bot_assigned_storm_and_reassign_do_not_notify_human(self):
        self.config = replace(self.config, wake_policy=replace(self.config.wake_policy, storm_per_hour=1))
        self.pipeline = self.make_pipeline()
        result = self.pipeline.handle_event(event(channel="C0000000001", bot_id="BDUMMY"))
        self.store._update(result.message_id, {"assigned_at": event()["received_at"]})
        sweep = TimerSweep(self.config, self.store, self.receivers, self.clock, human=self.pipeline.human)
        sweep.run_once(100000)
        self.pipeline.flush(100000)
        self.receivers[-1].notify.assert_not_called()
        self.assertEqual(self.store._rows("SELECT * FROM human_notices"), [])

    def test_human_session_bypasses_both_switches_lane_does_not(self):
        self.store.upsert_lane("human_queue", session_pointer="dummy-human-session")
        (self.root / "state").mkdir()
        (self.root / "state/wake.off").touch()
        base = self.config.notify_adapters[-1]
        self.config = replace(self.config, wake_policy=replace(self.config.wake_policy, enabled=False),
                              notify_adapters=tuple(replace(base, kind="session_message", purpose=purpose,
                                             command_template=("dummy", "{target}", "{message}"))
                                                    for purpose in ("lane", "human_queue")))
        self.receivers = build_notifiers(self.config, self.store)
        runner = Mock(return_value=SimpleNamespace(returncode=0))
        for notifier in self.receivers:
            notifier.runner = runner
        self.pipeline = self.make_pipeline()
        result = self.pipeline.handle_event(event(text="[support] <@U0000000093>"))
        runner.assert_called_once()
        self.assertIn(result.message_id, runner.call_args.args[0][-1])
        self.assertIn("FYI", runner.call_args.args[0][-1])
        self.assertIsNone(self.receivers[0].target_resolver("support"))
        self.assertEqual(self.receivers[1].target_resolver("human_queue"), "dummy-human-session")

    def test_human_adapter_dry_run_keeps_pending_receipt(self):
        self.config = replace(self.config, notify_adapters=tuple(replace(spec, dry_run=True)
                                                                for spec in self.config.notify_adapters))
        self.pipeline = self.make_pipeline()
        self.pipeline.handle_event(event(text="<@U0000000093>"))
        self.receivers[-1].notify.assert_not_called()
        self.assertIsNone(self.store._rows("SELECT * FROM human_notices")[0]["sent_at"])

    def test_digest_and_timers_share_batch_receipts_survive_restart(self):
        self.use_drop()
        cfg = replace(self.config, timers=replace(self.config.timers, unassigned_seconds=5))
        self.pipeline.handle_event(event(text="plain"))
        self.now = 1005
        sweep = TimerSweep(cfg, self.store, self.receivers, self.clock, human=self.pipeline.human)
        self.assertEqual(sweep.run_once(), 0)
        self.assertEqual(self.store._rows("SELECT * FROM timer_events"), [])
        self.now = 1010
        self.pipeline.flush(self.now)
        self.assertEqual(len(self.drops()), 1)
        self.assertEqual(self.drops()[0]["count"], 1)
        self.assertEqual(self.drops()[0]["human_queue"]["timers"], ["unassigned"])
        self.assertEqual(len(self.store._rows("SELECT * FROM timer_events")), 1)
        self.store.close()
        self.store.open()
        self.store.migrate()
        self.now = 2000
        restarted = TimerSweep(cfg, self.store, self.receivers, self.clock)
        restarted.run_once()
        self.assertEqual(len(self.drops()), 1)

    def test_reassign_digest_with_wake_off_retains_ownership(self):
        self.use_drop()
        result = self.pipeline.handle_event(event())
        self.store._update(result.message_id, {"assigned_at": event()["received_at"]})
        (self.root / "state").mkdir()
        (self.root / "state/wake.off").touch()
        sweep = TimerSweep(self.config, self.store, self.receivers, self.clock, human=self.pipeline.human)
        self.now = 1000 + self.config.timers.reassign_seconds
        sweep.run_once()
        self.assertEqual(self.drops(), [])
        sweep.run_once(self.now + 10)
        self.assertEqual(self.drops()[0]["human_queue"]["timers"], ["reassign"])
        self.assertIn("reassignment proposal=1", self.drops()[0]["body"])
        self.assertEqual(self.store.get_message(result.message_id)["lane"], "support")
        sweep.run_once(self.now + 100)
        self.assertEqual(len(self.drops()), 1)

    def test_failed_adapter_retry_does_not_repeat_successful_sibling(self):
        self.config = replace(self.config, notify_adapters=(self.config.notify_adapters[-1],) * 2)
        good, flaky = Mock(), Mock()
        flaky.notify.side_effect = [RuntimeError("private"), None]
        self.receivers = [good, flaky]
        self.pipeline = self.make_pipeline()
        self.pipeline.handle_event(event(text="plain"))
        with self.assertLogs("slack_dispatch.pipeline", level="ERROR"):
            self.pipeline.flush(1010)
        self.pipeline.flush(1011)
        good.notify.assert_called_once()
        self.assertEqual(flaky.notify.call_count, 2)

    def test_pending_digest_survives_restart_and_drops_resolved_items(self):
        first = self.pipeline.handle_event(event(text="plain"))
        second = self.pipeline.handle_event(event(2, text="plain"))
        self.store.set_state(first.message_id, "done")
        self.store.close()
        self.store.open()
        self.store.migrate()
        self.pipeline = self.make_pipeline()
        self.pipeline.flush(1010)
        self.receivers[-1].notify.assert_called_once_with("human_queue", [second.message_id])

    def test_binding_import_keep_replace_unknown_extra_keys(self):
        path = self.root / "bindings.jsonl"
        rows = [dict(source="slack", channel="CDUMMY", thread_ts="1", lane=lane, extra="ignored")
                for lane in ("support", "content", "unknown")]
        path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        output = io.StringIO()
        self.assertEqual(main(["import-bindings", str(path)], store=self.store, stdout=output), 0)
        self.assertIn("added=1 kept=1 skipped unknown lane=1", output.getvalue())
        self.assertIn("Unknown lanes: unknown", output.getvalue())
        self.assertEqual(self.store.lookup_binding("slack", "CDUMMY", "1"), "support")
        self.assertEqual(main(["import-bindings", str(path), "--replace"], store=self.store,
                              stdout=io.StringIO()), 0)
        self.assertEqual(self.store.lookup_binding("slack", "CDUMMY", "1"), "content")
        self.assertIsNone(self.store.get_lane("unknown"))

    def test_binding_import_validates_entire_file_before_mutation(self):
        path = self.root / "bindings.jsonl"
        path.write_text(json.dumps(dict(source="slack", channel="CDUMMY", thread_ts="1", lane="support"))
                        + "\n{}", encoding="utf-8")
        self.assertEqual(main(["import-bindings", str(path)], store=self.store, stderr=io.StringIO()), 2)
        self.assertIsNone(self.store.lookup_binding("slack", "CDUMMY", "1"))

    def test_socket_service_preserves_raw_bot_id_and_ack_order(self):
        cfg = replace(self.config, sources=load_config(SAMPLE).sources)
        calls, events = [], []
        def receive(row):
            calls.append("event")
            events.append(row)
        source = build_service_sources(cfg, receive, self.store)[0]
        source._response_class = lambda **kwargs: kwargs
        client = Mock()
        client.send_socket_mode_response.side_effect = lambda response: calls.append("ack")
        for number, raw in enumerate((dict(user="UDUMMY", bot_id="BDUMMY"), {}, dict(user="UDUMMY"))):
            request = SimpleNamespace(envelope_id="dummy", type="events_api", payload={
                "event_id": f"EvDUMMY{number}", "event": dict(type="message", channel="CDUMMY",
                                                            ts=str(number + 1), **raw)})
            source._receive(client, request)
        self.assertEqual(calls, ["ack", "event"] * 3)
        self.assertEqual([row["is_bot"] for row in events], [True, True, False])
        self.assertTrue(all(type(row["is_bot"]) is bool for row in events))

    def test_reconcile_service_preserves_bot_id_before_normalization(self):
        cfg = replace(self.config, sources=load_config(SAMPLE).sources)
        events = []
        source = build_service_sources(cfg, events.append, self.store)[1]
        self.store.set_cursor("slack", "CDUMMY", "1")
        source.client = Mock()
        source.client.api_call.return_value = {"messages": [dict(ts="2", user="UDUMMY", bot_id="BDUMMY")]}
        source.catch_up()
        self.assertTrue(events[0]["is_bot"])
        self.assertEqual(source._bots, {})


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.store = LedgerStore(":memory:")
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        self.now = 0
        self.source = SlackReconcileSource(SourceConfig("slack_reconcile", True, 3600,
                                                        discover_channels=True), Mock(),
                                            "dummy.env", "DUMMY", self.store,
                                            clock=lambda: self.now, excluded_channels=("CEXCLUDED",))
        self.source.client = Mock()
        self.source.client.api_call.side_effect = self.respond

    def respond(self, method, **kwargs):
        if method == "conversations.list":
            if kwargs["params"].get("cursor"):
                return {"channels": [{"id": "DDUMMY"}, {"id": "CEXCLUDED"}]}
            return {"channels": [{"id": "CDUMMY"}], "response_metadata": {"next_cursor": "dummy-next"}}
        return {"messages": []}

    def test_discovery_paginates_excludes_and_refreshes_at_six_hours(self):
        self.store.set_cursor("slack", "CEXCLUDED", "1")
        for now in (0, 21599, 21600):
            self.now = now
            self.source.catch_up()
        calls = self.source.client.api_call.call_args_list
        lists = [call for call in calls if call.args[0] == "conversations.list"]
        histories = [call for call in calls if call.args[0] == "conversations.history"]
        self.assertEqual(len(lists), 4)
        self.assertEqual(len(histories), 6)
        self.assertEqual({call.kwargs["params"]["channel"] for call in histories}, {"CDUMMY", "DDUMMY"})
        self.assertEqual(lists[0].kwargs["params"]["types"], "public_channel,private_channel,im,mpim")
        self.assertTrue(all(call.kwargs["http_verb"] == "GET" for call in calls))

    def test_disabled_discovery_does_not_list_and_still_excludes_known(self):
        self.source.config = replace(self.source.config, discover_channels=False)
        self.store.set_cursor("slack", "CEXCLUDED", "1")
        self.source.catch_up()
        self.source.client.api_call.assert_not_called()

    def test_standalone_factory_carries_global_exclusions(self):
        from slack_dispatch.ingest import build_sources
        cfg = load_config(SAMPLE)
        source = build_sources(cfg, Mock(), self.store)[1]
        self.assertEqual(source.excluded_channels, set(cfg.excluded_channels))

    def test_failed_discovery_retries_without_losing_known_scan(self):
        self.store.set_cursor("slack", "CKNOWN", "1")
        self.source.client.api_call.side_effect = [{"ok": False}, {"messages": []}]
        self.source.catch_up()
        self.assertEqual(self.source.failures, 1)
        self.assertIsNone(self.source._last_discovery)
        self.source.client.api_call.side_effect = self.respond
        self.source.catch_up()
        self.assertEqual(self.source._last_discovery, 0)
        self.assertEqual(self.source._discovered, {"CDUMMY", "DDUMMY"})


class AdditiveConfigTests(unittest.TestCase):
    def test_defaults_and_invalid_values(self):
        config = parse_config(raw_config())
        self.assertEqual(config.human_queue, HumanQueue())
        self.assertEqual(config.bot_messages.policy, "route_or_exclude")
        for key, value in (("human_queue", {"digest_seconds": False}),
                           ("human_queue", {"mode": "wrong"}),
                           ("human_queue", {"immediate_flags": "always_human"}),
                           ("bot_messages", {"policy": "wrong"}),
                           ("bot_messages", {"known_bot_users": "UDUMMY"})):
            raw = raw_config()
            raw[key] = value
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse_config(raw)
        raw = raw_config()
        raw["sources"][0]["discover_channels"] = "true"
        with self.assertRaises(ConfigError):
            parse_config(raw)

    def test_v1_migration_is_additive_and_repeatable(self):
        store = LedgerStore(":memory:")
        store.open()
        self.addCleanup(store.close)
        schema = (SAMPLE.parents[1].parent / "slack_dispatch/ledger/schema.sql").read_text(encoding="utf-8")
        schema = schema.replace("    attention TEXT,\n", "").replace("    is_bot INTEGER NOT NULL DEFAULT 0,\n", "")
        store._db.executescript(schema)
        store._db.execute("INSERT INTO schema_version VALUES (1)")
        store._db.execute("INSERT INTO lanes(id) VALUES ('dummy')")
        store._db.execute("INSERT INTO messages(id, source, channel, ts, is_thread_reply, received_at, state, lane) "
                          "VALUES ('slack:CDUMMY:1', 'slack', 'CDUMMY', '1', 0, '2026-01-01T00:00:00+00:00', 'assigned', 'dummy')")
        store.bind_thread("slack", "CDUMMY", "1", "dummy")
        store.migrate()
        store.migrate()
        self.assertEqual(store._rows("SELECT version FROM schema_version"), [{"version": 1}])
        columns = {row["name"] for row in store._rows("PRAGMA table_info(messages)")}
        self.assertTrue({"attention", "is_bot"} <= columns)
        self.assertEqual(store.get_message("slack:CDUMMY:1")["lane"], "dummy")
        self.assertEqual(store.get_message("slack:CDUMMY:1")["is_bot"], 0)
        self.assertEqual(store.lookup_binding("slack", "CDUMMY", "1"), "dummy")
