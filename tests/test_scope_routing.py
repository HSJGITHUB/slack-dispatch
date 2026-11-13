"""P13 items 3 and 6: ScopeClaim precedence, learning proposals, binding updates, cutover rerun."""
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from slack_dispatch.config.loader import ConfigError, parse_config
from slack_dispatch.console import actions
from slack_dispatch.inbox.cli import main as cli_main
from slack_dispatch.inbox.files import read
from slack_dispatch.ledger.store import LedgerStore, ProposalStateError
from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline
from slack_dispatch.router.classifier import NullSuggester
from slack_dispatch.router.engine import route
from slack_dispatch.router.rules import ScopeClaim
from tests.fakes.fake_store import message
from tests.test_pipeline_integration import event, fixture_config
from tests.test_router_config import raw_config


def claims(**table):
    """lookup_scope over {(kind, key): (lane, confirmed_by)}."""
    rows = {tuple(key.split("__")): value for key, value in table.items()}

    def lookup(kind, key):
        value = rows.get((kind, key))
        return {"lane": value[0], "confirmed_by": value[1]} if value else None
    return lookup


class PrecedenceTests(unittest.TestCase):
    def setUp(self):
        self.config = parse_config(raw_config())
        self.binding = Mock(return_value=None)

    def test_scope_claim_after_thread_inherit_before_tag_and_default(self):
        lookup = claims(channel__C0000000001=("content", "console"))
        row = message(text="[support]", thread_ts="1")
        self.binding.return_value = "owner"
        self.assertEqual(route(row, self.config, self.binding, lookup).rule, "thread_inherit")
        self.binding.return_value = None
        decision = route(row, self.config, self.binding, lookup)
        self.assertEqual((decision.lane, decision.reason, decision.rule),
                         ("content", "scope_claim", "scope_claim"))
        # Without a claim the explicit tag and then the channel default apply as before.
        self.assertEqual(route(row, self.config, self.binding, claims()).rule, "explicit_tag")
        self.assertEqual(route(message(text=None), self.config, self.binding, claims()).rule,
                         "channel_default")

    def test_auto_reason_dm_person_and_unconfigured_lanes(self):
        auto = claims(channel__C0000000005=("support", "scope:auto"))
        decision = route(message(channel="C0000000005", text=None), self.config, self.binding, auto)
        self.assertEqual((decision.lane, decision.reason, decision.rule), ("support", "scope:auto", "scope_claim"))
        lookup = claims(dm__D0000000001=("content", "console"), person__U0000000001=("support", "console"))
        dm = message(channel="D0000000001", channel_type="im", text=None)
        self.assertEqual(route(dm, self.config, self.binding, lookup).lane, "content")
        other_dm = message(channel="D0000000002", channel_type="im", text=None)
        self.assertEqual(route(other_dm, self.config, self.binding, lookup).lane, "support")
        # A person claim never applies outside direct conversations.
        channel = message(channel="C0000000006", text=None)
        self.assertEqual(route(channel, self.config, self.binding, lookup).rule, "none")
        ghost = claims(channel__C0000000006=("retired-lane", "console"))
        self.assertEqual(route(channel, self.config, self.binding, ghost).rule, "none")
        self.assertIsNone(ScopeClaim(None).match(channel))

    def test_excluded_and_bot_paths(self):
        lookup = claims(channel__C0000000003=("support", "console"), channel__C0000000007=("content", "console"))
        self.assertEqual(route(message(channel="C0000000003"), self.config, self.binding, lookup).rule,
                         "excluded")
        bot = message(channel="C0000000007", user=None, text="[support]")
        self.assertEqual(route(bot, self.config, self.binding, lookup).lane, "content")
        self.assertEqual(route(bot, self.config, self.binding, claims()).reason, "excluded:bot")

    def test_plain_lookup_callable_disables_scope_routing(self):
        # A Mock or plain function has no store behind it, so no ScopeClaim lookup happens.
        row = message(channel="C0000000006", text=None)
        self.assertEqual(route(row, self.config, self.binding).rule, "none")


class LedgerCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p13-route-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = fixture_config(self.root)
        self.store = LedgerStore(self.config.ledger.path)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id)
        self.inbox = self.config.inbox.root
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(),
                                 [FileInboxNotifier(self.inbox, self.store.get_message), Mock(), Mock()],
                                 WakePolicyEngine(self.config.wake_policy, lambda: 1000.0), lambda: 1000.0)

    def handle(self, number, **changes):
        return self.pipeline.handle_event(event(number, **changes))


class PipelineScopeTests(LedgerCase):
    def test_confirmed_claim_routes_through_the_pipeline_store(self):
        self.store.confirm_scope_claim("content", "channel", "C0000000002", by="console")
        result = self.handle(1, text="no tag here")
        self.assertEqual((result.state, result.lane), ("assigned", "content"))
        self.assertEqual(self.store.get_message(result.message_id)["route_reason"], "scope_claim")
        self.store.remove_scope_claim("content", "channel", "C0000000002")
        self.assertEqual(self.handle(2, text="no tag here").state, "human_pending")


class LearningTests(LedgerCase):
    def test_reroute_updates_binding_and_records_evidence(self):
        root = self.handle(1, text="[support] root")
        self.assertEqual(self.store.lookup_binding("slack", "C0000000002", "1000.000001"), "support")
        actions.reroute(self.store, self.inbox, root.message_id, "content", source="cli")
        self.assertEqual(self.store.lookup_binding("slack", "C0000000002", "1000.000001"), "content")
        reply = self.handle(2, text="a reply", thread_ts="1000.000001", is_thread_reply=True)
        self.assertEqual((reply.lane, self.store.get_message(reply.message_id)["route_reason"]),
                         ("content", "thread_inherit"))
        row = self.store.get_scope_proposal("channel", "C0000000002", "content")
        self.assertEqual((row["evidence_count"], row["status"], row["last_evidence"]), (1, "open", "reroute"))
        # Rerouting the reply moves the whole thread again; same lane twice reaches suggested.
        actions.reroute(self.store, self.inbox, reply.message_id, "owner")
        self.assertEqual(self.store.lookup_binding("slack", "C0000000002", "1000.000001"), "owner")
        actions.reroute(self.store, self.inbox, root.message_id, "content")  # same lane: no event
        self.assertEqual(self.store.get_scope_proposal("channel", "C0000000002", "content")["evidence_count"], 1)
        actions.reroute(self.store, self.inbox, reply.message_id, "content")
        row = self.store.get_scope_proposal("channel", "C0000000002", "content")
        self.assertEqual(row["evidence_count"], 2)
        self.assertEqual((row["status"], row["conflict"]), ("open", 1))  # owner also has evidence
        # Proposals never route: a new root still has no default.
        self.assertEqual(self.handle(3, text="no tag").state, "human_pending")

    def test_cli_reroute_and_assign_share_the_action_path(self):
        pending = self.handle(1, text="no tag")
        self.assertEqual(pending.state, "human_pending")
        out, err = io.StringIO(), io.StringIO()
        code = cli_main(["--inbox-root", self.inbox, "assign", pending.message_id, "support"],
                        store=self.store, stdout=out, stderr=err)
        self.assertEqual(code, 0)
        self.assertEqual(self.store.lookup_binding("slack", "C0000000002", "1000.000001"), "support")
        self.assertEqual(self.store.get_scope_proposal("channel", "C0000000002", "support")["last_evidence"],
                         "assign")
        # Assigning an item that is not human_pending is no learning evidence.
        tagged = self.handle(2, text="[content] tagged")
        actions.assign(self.store, self.inbox, tagged.message_id, "owner", reason="human:card")
        self.assertIsNone(self.store.get_scope_proposal("channel", "C0000000002", "owner"))
        self.assertEqual(self.store.lookup_binding("slack", "C0000000002", "1000.000002"), "owner")
        code = cli_main(["--inbox-root", self.inbox, "reroute", tagged.message_id, "--to", "support"],
                        store=self.store, stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(self.store.lookup_binding("slack", "C0000000002", "1000.000002"), "support")
        row = self.store.get_scope_proposal("channel", "C0000000002", "support")
        self.assertEqual((row["evidence_count"], row["status"]), (2, "suggested"))

    def test_dm_items_learn_dm_keys_and_threshold_is_configurable(self):
        pending = self.handle(1, text="hello", channel="D0000000077", channel_type="im")
        actions.assign(self.store, self.inbox, pending.message_id, "owner", min_evidence=1)
        row = self.store.get_scope_proposal("dm", "D0000000077", "owner")
        self.assertEqual((row["evidence_count"], row["status"]), (1, "suggested"))


class DecisionTests(LedgerCase):
    def test_confirm_reject_defer_and_supersede(self):
        store = self.store
        for _ in range(2):
            store.record_scope_evidence("channel", "C0000000031", "support", "reroute")
        self.assertEqual(store.get_scope_proposal("channel", "C0000000031", "support")["status"], "suggested")
        store.decide_scope_proposal("channel", "C0000000031", "support", "confirm")
        self.assertEqual(store.lookup_scope_claim("channel", "C0000000031")["lane"], "support")
        with self.assertRaises(ProposalStateError):
            store.decide_scope_proposal("channel", "C0000000031", "support", "reject")
        with self.assertRaises(KeyError):
            store.decide_scope_proposal("channel", "C0000000031", "owner", "confirm")
        store.record_scope_evidence("channel", "C0000000031", "content", "reroute")
        row = store.get_scope_proposal("channel", "C0000000031", "content")
        self.assertEqual((row["status"], row["conflict"]), ("open", 1))
        store.decide_scope_proposal("channel", "C0000000031", "content", "confirm")
        self.assertEqual(store.lookup_scope_claim("channel", "C0000000031")["lane"], "content")
        self.assertEqual(store.get_scope_proposal("channel", "C0000000031", "support")["status"], "superseded")
        support = [row for row in store.list_scope_claims("support") if row["key"] == "C0000000031"]
        self.assertEqual(support[0]["status"], "claimed")
        # Defer hides until new evidence; reject sticks and returns an auto claim to claimed.
        store.record_scope_evidence("dm", "D0000000031", "owner", "sent")
        store.decide_scope_proposal("dm", "D0000000031", "owner", "defer")
        self.assertEqual(store.get_scope_proposal("dm", "D0000000031", "owner")["status"], "deferred")
        store.record_scope_evidence("dm", "D0000000031", "owner", "sent")
        self.assertEqual(store.get_scope_proposal("dm", "D0000000031", "owner")["status"], "suggested")
        store.add_scope_claim("owner", "channel", "C0000000032")
        store.record_scope_evidence("channel", "C0000000032", "owner", "scope")
        store.confirm_scope_claim("owner", "channel", "C0000000032", by="scope:auto")
        store.decide_scope_proposal("channel", "C0000000032", "owner", "reject")
        self.assertIsNone(store.lookup_scope_claim("channel", "C0000000032"))
        store.record_scope_evidence("channel", "C0000000032", "owner", "reroute")
        self.assertEqual(store.get_scope_proposal("channel", "C0000000032", "owner")["status"], "rejected")
        with self.assertRaises(ValueError):
            store.decide_scope_proposal("channel", "C0000000032", "owner", "maybe")

    def test_scope_learning_config(self):
        raw = raw_config()
        self.assertEqual(parse_config(raw).scope_learning.min_evidence, 2)
        raw["scope_learning"] = {"min_evidence": 3}
        self.assertEqual(parse_config(raw).scope_learning.min_evidence, 3)
        for bad in ({"min_evidence": 0}, {"min_evidence": "2"}, ["x"]):
            raw["scope_learning"] = bad
            with self.assertRaises(ConfigError):
                parse_config(raw)
        config = replace(self.config, scope_learning=replace(self.config.scope_learning, min_evidence=5))
        self.assertEqual(config.scope_learning.min_evidence, 5)


class CutoverRerunTests(LedgerCase):
    def test_rerun_skips_items_with_an_unconsumed_backfill_record(self):
        first = self.handle(1, text="[support] one").message_id
        second = self.handle(2, text="[support] two").message_id

        def cutover():
            out = io.StringIO()
            code = cli_main(["--inbox-root", self.inbox, "cutover", "support", "--apply"],
                            store=self.store, stdout=out, stderr=io.StringIO())
            self.assertEqual(code, 0)
            return out.getvalue()

        def backfills(mid):
            return sum(1 for row in read(self.inbox, "support", include_done=True)
                       if row["id"] == mid and row["kind"] == "backfill")

        self.assertIn("redelivered=2", cutover())
        out = cutover()
        self.assertIn("kept_open=2 redelivered=0", out)
        self.assertIn("already_backfilled=2", out)
        self.assertEqual((backfills(first), backfills(second)), (1, 1))
        # Done, then reopened by a reroute back: the old record was consumed, so it is sent again.
        self.store.set_state(first, "done", note="lane done")
        actions.reroute(self.store, self.inbox, first, "content")
        actions.reroute(self.store, self.inbox, first, "support")
        out = cutover()
        self.assertEqual(backfills(first), 2)
        self.assertEqual(backfills(second), 1)


if __name__ == "__main__":
    unittest.main()
