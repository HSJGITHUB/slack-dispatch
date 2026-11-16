"""P15 member-only intake: membership cache, pipeline gate, reconcile scan set, service wiring.

Offline only: a fake Slack client answers paginated users.conversations and history
requests. Dummy ids only.
"""
from dataclasses import replace
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from slack_dispatch import service
from slack_dispatch.config.loader import ConfigError, load_config, parse_config
from slack_dispatch.config.schema import Membership, SourceConfig
from slack_dispatch.ingest.membership import NOT_MEMBER, MembershipCache, member_status
from slack_dispatch.ingest.slack_reconcile import SlackReconcileSource
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline, routing_config
from slack_dispatch.router.classifier import NullSuggester
from slack_dispatch.router.engine import route
from slack_dispatch.runtime import read_heartbeat
from tests.test_console_core import sample_data
from tests.test_pipeline_integration import SAMPLE, event, fixture_config

MEMBER = "C0000000001"
OTHER = "C0000000002"
PRIVATE = "G0000000001"
DM = "D0000000001"
GROUP = "G0000000002"
EXTRA = ["C0000000011", "C0000000012", "C0000000013", "C0000000014", "C0000000015", "C0000000016"]

PAGES = [
    {"ok": True, "channels": [
        {"id": MEMBER, "name": "dummy-support", "is_channel": True},
        {"id": PRIVATE, "name": "dummy-private", "is_group": True, "is_private": True}],
     "response_metadata": {"next_cursor": "dummy-page-2"}},
    {"ok": True, "channels": [
        {"id": DM, "is_im": True, "user": "U0000000001"},
        {"id": GROUP, "name": "mpdm-dummy", "is_mpim": True, "is_private": True},
        {"id": "not-an-id", "name": "skipped"}],
     "response_metadata": {"next_cursor": ""}},
]


class SlackApiError(Exception):
    """Stand-in for the SDK error: carries a response with data."""

    def __init__(self, code):
        super().__init__("dummy failure text")
        self.response = Mock(status_code=400, data={"ok": False, "error": code})


class FakeClient:
    """Paginated users.conversations plus empty history and replies."""

    def __init__(self, pages=None, history=None):
        self.pages = list(pages or PAGES)
        self.history = history or {}
        self.calls = []
        self.fail = None

    def api_call(self, method, http_verb="GET", params=None):
        params = dict(params or {})
        self.calls.append((method, params))
        if method == "users.conversations":
            if self.fail:
                raise SlackApiError(self.fail)
            index = 0 if not params.get("cursor") else int(params["cursor"].rsplit("-", 1)[1]) - 1
            return self.pages[index]
        if method == "conversations.history":
            return {"ok": True, "messages": self.history.get(params["channel"], [])}
        return {"ok": True, "messages": []}

    def methods(self, name):
        return [params for method, params in self.calls if method == name]


def memory_store():
    store = LedgerStore(":memory:", store_body=True)
    store.open()
    store.migrate()
    return store


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.store = memory_store()
        self.addCleanup(self.store.close)
        self.client = FakeClient()
        self.now = 1000.0
        self.cache = MembershipCache(self.client, self.store, 3600, clock=lambda: self.now)

    def test_paginated_load_into_ledger_table(self):
        with self.assertLogs("slack_dispatch.ingest.membership", level="INFO") as logs:
            self.assertTrue(self.cache.refresh())
        calls = self.client.methods("users.conversations")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["types"], "public_channel,private_channel,mpim,im")
        self.assertEqual(calls[1]["cursor"], "dummy-page-2")
        rows = {row["channel"]: row for row in self.store.list_membership()}
        self.assertEqual(set(rows), {MEMBER, PRIVATE, DM, GROUP})
        self.assertEqual({key: rows[key]["kind"] for key in rows},
                         {MEMBER: "public_channel", PRIVATE: "private_channel", DM: "im", GROUP: "mpim"})
        self.assertEqual(rows[DM]["partner_user"], "U0000000001")
        self.assertEqual(rows[MEMBER]["name"], "dummy-support")
        self.assertTrue(all(row["is_member"] == 1 and row["seen_at"] for row in rows.values()))
        self.assertEqual(self.cache.member_channels(), {MEMBER, PRIVATE, DM, GROUP})
        self.assertEqual(sum("Membership cache available" in line for line in logs.output), 1)
        self.assertTrue(self.cache.is_member(DM))
        self.assertFalse(self.cache.is_member(OTHER))

    def test_refresh_failure_keeps_previous_table_and_logs_one_warning(self):
        self.cache.refresh()
        before = self.store.list_membership()
        self.client.fail = "ratelimited"
        self.now += 3600
        with self.assertLogs("slack_dispatch.ingest.membership", level="WARNING") as logs:
            self.assertFalse(self.cache.maybe_refresh())
        self.assertEqual(len(logs.output), 1)
        self.assertIn("class=SlackApiError error=ratelimited", logs.output[0])
        self.assertNotIn("dummy failure text", logs.output[0])
        self.assertEqual(self.store.list_membership(), before)
        self.assertEqual(self.cache.member_channels(), {MEMBER, PRIVATE, DM, GROUP})
        self.assertEqual(self.cache.failures, 1)
        self.assertFalse(self.cache.maybe_refresh())  # not due again until refresh_seconds pass

    def test_empty_refresh_keeps_previous_members(self):
        self.cache.refresh()
        before = self.store.list_membership()
        self.client.pages = [{"ok": True, "channels": [], "response_metadata": {"next_cursor": ""}}]
        self.now += 3600
        with self.assertLogs("slack_dispatch.ingest.membership", level="WARNING") as logs:
            self.assertFalse(self.cache.maybe_refresh())
        self.assertEqual(logs.output, ["WARNING:slack_dispatch.ingest.membership:"
                                       "membership refresh returned no channels; keeping previous table"])
        self.assertEqual(self.store.list_membership(), before)
        self.assertTrue(self.cache.is_member(MEMBER))
        self.assertFalse(self.cache.is_member(OTHER))
        self.assertEqual(self.cache.failures, 1)

    def test_left_channel_keeps_row_as_non_member_and_due_interval(self):
        self.cache.refresh()
        self.assertFalse(self.cache.due(self.now + 3599))
        self.assertTrue(self.cache.due(self.now + 3600))
        self.client.pages = [{"ok": True, "channels": [{"id": PRIVATE, "is_private": True}]}]
        self.now += 3600
        self.assertTrue(self.cache.maybe_refresh())
        rows = {row["channel"]: row["is_member"] for row in self.store.list_membership()}
        self.assertEqual(rows, {MEMBER: 0, PRIVATE: 1, DM: 0, GROUP: 0})
        self.assertFalse(self.cache.is_member(MEMBER))

    def test_previous_table_loads_without_network_and_missing_client_is_skipped(self):
        self.cache.refresh()
        calls = len(self.client.calls)
        fresh = MembershipCache(lambda: None, self.store, 3600, clock=lambda: self.now)
        self.assertIsNone(fresh.is_member(OTHER))
        self.assertTrue(fresh.load())
        self.assertFalse(fresh.is_member(OTHER))
        self.assertFalse(fresh.refresh())  # no client yet: skipped silently
        self.assertEqual(fresh.failures, 0)
        self.assertEqual(len(self.client.calls), calls)

    def test_startup_report_of_configured_non_member_channels_once(self):
        cache = MembershipCache(self.client, self.store, 3600, clock=lambda: self.now,
                                configured_channels=[MEMBER, OTHER, "D0000000009", *EXTRA])
        with self.assertLogs("slack_dispatch.ingest.membership", level="INFO") as logs:
            cache.refresh()
            self.now += 3600
            cache.refresh()
        warnings = [line for line in logs.output if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 1)
        first = ", ".join(sorted([OTHER, *EXTRA])[:5])
        self.assertIn("7 configured channels are not member channels: " + first + " (first 5)", warnings[0])
        self.assertNotIn(sorted(EXTRA)[-1], warnings[0])

    def test_member_status_rules(self):
        self.assertIsNone(member_status(OTHER, set(), False))
        self.assertTrue(member_status(MEMBER, {MEMBER}, True))
        self.assertFalse(member_status(OTHER, {MEMBER}, True))
        self.assertTrue(member_status("D0000000002", {MEMBER}, True))
        self.assertTrue(member_status(GROUP, {MEMBER}, True, "mpim"))
        with self.assertRaises(ValueError):
            MembershipCache(self.client, self.store, policy="dummy")


class ConfigTests(unittest.TestCase):
    def test_section_defaults_and_validation(self):
        data = sample_data()
        self.assertEqual(parse_config(data).membership, Membership(True, 3600, "exclude"))
        del data["membership"]
        self.assertEqual(parse_config(data).membership, Membership())
        data["membership"] = None
        self.assertEqual(parse_config(data).membership, Membership())
        data["membership"] = {"policy": "off", "refresh_seconds": 600, "enabled": False}
        self.assertEqual(parse_config(data).membership, Membership(False, 600, "off"))
        for bad in ({"policy": "drop"}, {"refresh_seconds": 0}, {"enabled": "yes"}, []):
            with self.subTest(bad=bad):
                data["membership"] = bad
                with self.assertRaises(ConfigError):
                    parse_config(data)


class GateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-membership-")
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
                                 WakePolicyEngine(self.config.wake_policy, lambda: 1000.0), lambda: 1000.0)
        self.cache = MembershipCache(FakeClient(), self.store, 3600, clock=lambda: 1000.0)
        self.pipeline.membership = self.cache

    def test_non_member_channel_is_recorded_excluded_and_never_routed(self):
        self.cache.refresh()
        result = self.pipeline.handle_event(event(1, channel=OTHER, text="unmatched"))
        row = self.store.get_message(result.message_id)
        self.assertEqual((result.state, row["state"], row["note"]), ("excluded", "excluded", NOT_MEMBER))
        self.assertIsNone(row["lane"])
        self.lane.notify.assert_not_called()
        self.human.notify.assert_not_called()
        self.assertFalse((self.root / "state/human_queue.jsonl").exists())
        counters = self.pipeline.snapshot()
        self.assertEqual((counters["routed"], counters["human_pending"]), (0, 0))
        self.assertEqual((counters["excluded_not_member"], counters["member_channels"]), (1, 4))

    def test_config_excluded_channel_is_blocked_but_not_counted(self):
        self.cache.refresh()
        self.pipeline.config = replace(self.config, excluded_channels=(OTHER,))
        result = self.pipeline.handle_event(event(1, channel=OTHER, text="unmatched"))
        self.assertEqual(result.state, "excluded")
        self.lane.notify.assert_not_called()
        self.assertEqual(self.pipeline.snapshot()["excluded_not_member"], 0)

    def test_member_channel_routes_as_before(self):
        self.cache.refresh()
        result = self.pipeline.handle_event(event(1, channel=MEMBER))
        self.assertEqual((result.state, result.lane), ("assigned", "support"))
        self.lane.notify.assert_called_once_with("support", [result.message_id])
        self.assertEqual(self.pipeline.snapshot()["excluded_not_member"], 0)

    def test_policy_off_and_empty_cache_keep_todays_behaviour(self):
        # Cache never loaded: unknown channels fall back to normal routing.
        result = self.pipeline.handle_event(event(1, channel=OTHER))
        self.assertEqual((result.state, result.lane), ("assigned", "support"))
        self.cache.refresh()
        self.cache.policy = "off"
        result = self.pipeline.handle_event(event(2, channel=OTHER))
        self.assertEqual((result.state, result.lane), ("assigned", "support"))
        self.assertEqual(self.pipeline.snapshot()["excluded_not_member"], 0)

    def test_direct_conversations_count_as_members(self):
        self.cache.refresh()
        known = self.pipeline.handle_event(event(1, channel=DM, channel_type="im", text="dummy"))
        unseen = self.pipeline.handle_event(event(2, channel="D0000000002", channel_type="im", text="dummy"))
        for result in (known, unseen):
            self.assertNotEqual(self.store.get_message(result.message_id)["note"], NOT_MEMBER)

    def test_without_membership_snapshot_is_unchanged(self):
        self.pipeline.membership = None
        self.assertNotIn("member_channels", self.pipeline.snapshot())
        self.assertNotIn("excluded_not_member", self.pipeline.snapshot())

    def test_ledger_channel_exclusion_is_honoured_by_route(self):
        message = dict(source="slack", channel=MEMBER, ts="1000.000001", user="U0000000088",
                       text="[support] dummy")
        self.assertEqual(route(message, routing_config(self.config, self.store),
                               self.store.lookup_binding).lane, "support")
        self.assertTrue(self.store.add_channel_exclusion(MEMBER, "console"))
        self.assertFalse(self.store.add_channel_exclusion(MEMBER, "console"))
        decision = route(message, routing_config(self.config, self.store), self.store.lookup_binding)
        self.assertEqual((decision.rule, decision.lane), ("excluded", None))
        result = self.pipeline.handle_event(event(1, channel=MEMBER))
        self.assertEqual(result.state, "excluded")
        self.assertEqual(self.store.get_message(result.message_id)["note"], "excluded")
        self.assertTrue(self.store.remove_channel_exclusion(MEMBER))
        self.assertFalse(self.store.remove_channel_exclusion(MEMBER))
        self.assertIs(routing_config(self.config, self.store), self.config)
        self.assertEqual(self.pipeline.handle_event(event(2, channel=MEMBER)).lane, "support")
        rows = self.store.list_channel_exclusions(include_removed=True)
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["removed_at"])


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.store = memory_store()
        self.addCleanup(self.store.close)
        for channel in (MEMBER, OTHER, PRIVATE):
            self.store.set_cursor("slack", channel, "999000")
        self.client = FakeClient(history={MEMBER: [dict(ts="999500", user="U0000000088")],
                                          OTHER: [dict(ts="999600", user="U0000000088")]})
        self.events = []
        self.source = self.make_source()

    def make_source(self, **changes):
        config = SourceConfig("slack_reconcile", True, 3600, replies=False, **changes)
        source = SlackReconcileSource(config, self.events.append, "dummy.env", "DUMMY", self.store,
                                      clock=lambda: 1000000)
        source.client = self.client
        source.membership = MembershipCache(lambda: source.client, self.store, 3600,
                                            clock=lambda: 1000000)
        return source

    def scanned(self):
        return [params["channel"] for params in self.client.methods("conversations.history")]

    def test_pass_refreshes_with_the_same_client_and_scans_member_channels_only(self):
        self.source.catch_up()
        self.assertEqual(self.client.calls[0][0], "users.conversations")
        self.assertEqual(sorted(self.scanned()), [MEMBER, PRIVATE])
        self.assertEqual([row["channel"] for row in self.events], [MEMBER])
        self.assertEqual(self.source.summary["channels"], 2)

    def test_policy_off_empty_cache_and_ledger_exclusions(self):
        self.source.membership.policy = "off"
        self.source.catch_up()
        self.assertEqual(sorted(self.scanned()), [MEMBER, OTHER, PRIVATE])
        self.client.calls.clear()
        self.source.membership = None
        self.store.add_channel_exclusion(PRIVATE, "console")
        self.source.catch_up()
        self.assertEqual(sorted(self.scanned()), [MEMBER, OTHER])

    def test_discovery_uses_the_cache_instead_of_conversations_list(self):
        source = self.make_source(discover_channels=True)
        source.catch_up()
        self.assertEqual(self.client.methods("conversations.list"), [])
        self.assertEqual(sorted(self.scanned()), sorted([DM, MEMBER, GROUP, PRIVATE]))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        # The service configures the package logger (propagate off); restore it for later tests.
        logger = logging.getLogger("slack_dispatch")
        self.addCleanup(setattr, logger, "propagate", logger.propagate)
        self.addCleanup(logger.setLevel, logger.level)
        temp = tempfile.TemporaryDirectory(prefix="dispatch-membership-service-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = replace(fixture_config(self.root), sources=load_config(SAMPLE).sources)
        self.client = FakeClient()

    def run_once(self):
        client = self.client

        def start(source):
            source.client = client
            source.catch_up()

        def stop(source):
            source.client = None

        with patch.object(service, "load_config", return_value=self.config), \
             patch.object(service, "build_notifiers", return_value=[Mock(), Mock(), Mock()]), \
             patch.object(SlackReconcileSource, "start", start), \
             patch.object(SlackReconcileSource, "stop", stop), \
             patch.object(service.time, "time", return_value=1000000.0), \
             patch.object(service.time, "sleep", return_value=None):
            return service.main(["--config", "dummy.yaml", "--once"])

    def test_once_refreshes_membership_and_reports_heartbeat_counters(self):
        self.assertEqual(self.run_once(), 0)
        counters = read_heartbeat(self.root / "state/heartbeat.json")["counters"]
        self.assertEqual((counters["member_channels"], counters["excluded_not_member"]), (4, 0))
        self.assertEqual(len(self.client.methods("users.conversations")), 2)
        log = (self.root / "logs/service.log").read_text(encoding="utf-8")
        self.assertIn("Membership cache available member_channels=4", log)
        self.assertIn("configured channels are not member channels: " + OTHER, log)

    def test_disabled_membership_builds_no_cache(self):
        self.config = replace(self.config, membership=Membership(enabled=False))
        self.assertEqual(self.run_once(), 0)
        counters = read_heartbeat(self.root / "state/heartbeat.json")["counters"]
        self.assertNotIn("member_channels", counters)
        self.assertEqual(self.client.methods("users.conversations"), [])

    def test_build_membership_needs_a_reconcile_source(self):
        self.assertIsNone(service.build_membership(self.config, Mock(), [Mock()], lambda: 0))


if __name__ == "__main__":
    unittest.main()
