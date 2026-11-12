import unittest
from unittest.mock import Mock

from slack_dispatch.config.loader import parse_config
from slack_dispatch.router.apply import apply_decision
from slack_dispatch.router.classifier import Suggestion
from slack_dispatch.router.engine import Decision, route
from tests.fakes.fake_store import FakeStore, message
from tests.test_router_config import raw_config


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.row = message(thread_ts="1")
        self.store = FakeStore([self.row])
        self.suggester = Mock(enabled=True)
        self.suggester.suggest.return_value = Suggestion("support", "Relevant work.", "v1+p1")

    def test_assigned_reply_binds_and_next_reply_inherits(self):
        for rule in ("explicit_tag", "channel_default", "person_channel", "thread_inherit"):
            with self.subTest(rule=rule):
                self.store.bindings.clear()
                outcome = apply_decision(self.store, self.row["id"],
                                         Decision("support", rule, rule), self.suggester)
                self.assertEqual(outcome.state, "assigned")
                self.assertEqual(self.store.lookup_binding("slack", self.row["channel"], "1"),
                                 "support")
        later = message(ts="3", thread_ts="1", text="[content]")
        self.assertEqual(route(later, parse_config(raw_config()), self.store.lookup_binding).rule,
                         "thread_inherit")
        self.suggester.suggest.assert_not_called()

    def test_existing_binding_preserved(self):
        self.store.bind_thread("slack", self.row["channel"], "1", "owner")
        apply_decision(self.store, self.row["id"], Decision("support", "tag", "explicit_tag"), None)
        self.assertEqual(self.store.lookup_binding("slack", self.row["channel"], "1"), "owner")

    def test_excluded_and_protected(self):
        for rule, state in (("excluded", "excluded"), ("always_human", "human_pending")):
            outcome = apply_decision(self.store, self.row["id"], Decision(None, rule, rule),
                                     self.suggester)
            self.assertEqual(outcome.state, state)
            self.assertEqual(self.store.messages[self.row["id"]]["state"], state)
        self.suggester.suggest.assert_not_called()
        self.assertFalse(self.store.bindings)

    def test_suggestion_never_assigns(self):
        outcome = apply_decision(self.store, self.row["id"], Decision(None, "none", "none"),
                                 self.suggester)
        self.assertEqual(outcome.suggested_lane, "support")
        self.assertEqual(outcome.state, "human_pending")
        self.assertIsNone(outcome.lane)
        self.assertNotIn("lane", self.store.messages[self.row["id"]])
        self.assertFalse(self.store.bindings)
        self.assertEqual(self.store.messages[self.row["id"]]["classifier_version"], "v1+p1")

    def test_disabled_missing_and_empty_suggester(self):
        self.suggester.enabled = False
        for suggester in (None, self.suggester):
            result = apply_decision(self.store, self.row["id"], Decision(None, "none", "none"),
                                    suggester)
            self.assertIsNone(result.suggested_lane)
        self.suggester.suggest.assert_not_called()
        self.suggester.enabled = True
        self.suggester.suggest.return_value = None
        self.assertIsNone(apply_decision(self.store, self.row["id"],
                                        Decision(None, "none", "none"), self.suggester).suggested_lane)

    def test_classifier_failure_keeps_human_queue(self):
        self.suggester.suggest.side_effect = RuntimeError("Fake failure")
        with self.assertRaises(RuntimeError):
            apply_decision(self.store, self.row["id"], Decision(None, "none", "none"), self.suggester)
        self.assertEqual(self.store.messages[self.row["id"]]["state"], "human_pending")

    def test_root_and_missing_message(self):
        self.store.messages[self.row["id"]]["thread_ts"] = self.row["ts"]
        apply_decision(self.store, self.row["id"], Decision("support", "tag", "explicit_tag"), None)
        self.assertFalse(self.store.bindings)
        with self.assertRaises(KeyError):
            apply_decision(self.store, "missing", Decision(None, "none", "none"), None)


class MergedFakeTests(unittest.TestCase):
    def test_list_messages_orders_before_limit_in_both_modes(self):
        early = "2026-01-01T00:00:00+00:00"
        late = "2026-01-02T00:00:00+00:00"
        rows = [message(id="dummy:c", received_at=late, lane="support"),
                message(id="dummy:b", received_at=early, lane="support"),
                message(id="dummy:a", received_at=early, lane="support"),
                message(id="dummy:d", received_at=early, lane="other", state="done")]
        for seeded in (True, False):
            with self.subTest(seeded=seeded):
                store = FakeStore(messages=rows) if seeded else FakeStore(path="dummy.sqlite3")
                if not seeded:
                    store.messages.update({row["id"]: dict(row) for row in rows})
                self.assertEqual([row["id"] for row in store.list_messages()],
                                 ["dummy:a", "dummy:b", "dummy:d", "dummy:c"])
                selected = store.list_messages(state="new", lane="support", since=early, limit=2)
                self.assertEqual([row["id"] for row in selected], ["dummy:a", "dummy:b"])
                self.assertEqual("text" in selected[0], seeded)
                self.assertEqual([row["id"] for row in store.list_messages(since=late)], ["dummy:c"])
                self.assertEqual(store.list_messages(limit=0), [])

    def test_seeded_and_path_modes_preserve_body_semantics(self):
        row = message(body="DUMMY_BODY", nested={"value": 1})
        for store in (FakeStore([row]), FakeStore(messages=[row])):
            self.assertEqual(store.get_message(row["id"])["body"], "DUMMY_BODY")
            self.assertNotIn("body", store.get_message(row["id"], include_body=False))
            self.assertNotIn("text", store.get_message(row["id"], include_body=False))
            self.assertEqual(store.get_message(row["id"], include_body=True)["body"], "DUMMY_BODY")
            self.assertIsNone(store.get_message("missing", include_body=False))
            returned = store.get_message(row["id"])
            returned["nested"]["value"] = 2
            self.assertEqual(store.get_message(row["id"])["nested"]["value"], 1)
            store.set_state(row["id"], "acked")
            self.assertEqual(store.calls[-1], ("set_state", row["id"], "acked"))
        store = FakeStore(path="dummy.sqlite3")
        store.messages[row["id"]] = row
        self.assertNotIn("body", store.get_message(row["id"]))
        self.assertNotIn("body", store.get_message(row["id"], include_body=False))
        self.assertNotIn("text", store.get_message(row["id"], include_body=False))
        self.assertNotIn("text", store.list_messages()[0])
        self.assertEqual(store.get_message(row["id"], include_body=True)["body"], "DUMMY_BODY")
