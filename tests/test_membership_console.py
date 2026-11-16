"""P15 console: Exclude channel action, ledger channel exclusions and the not-member badge.

Dummy ids only; FastAPI TestClient against a temp ledger.
"""
from pathlib import Path
import unittest

from slack_dispatch.console import queries
from slack_dispatch.pipeline import routing_config
from slack_dispatch.router.engine import route
from tests.test_console_app import ConsoleCase, stamp, ts
from tests.test_console_core import seed
from tests.test_console_staging import dictionary_keys

STATIC = Path(queries.__file__).with_name("static")
CHANNEL = "C0000000002"
OTHER = "C0000000001"


class ExcludeChannelTests(ConsoleCase):
    def seed_channel(self):
        pending = [self.pending(offset) for offset in (1, 2, 3)]
        assigned = seed(self.store, ts(4), received=stamp(4))
        self.store.set_route(assigned, "support", "channel_default")
        done = seed(self.store, ts(5), received=stamp(5))
        self.store.set_state(done, "done", note="dummy")
        other = seed(self.store, ts(6), channel=OTHER, received=stamp(6))
        self.store.set_state(other, "human_pending", note="none")
        return pending, assigned, done, other

    def test_commit_closes_open_items_and_writes_the_override(self):
        pending, assigned, done, other = self.seed_channel()
        data = self.post("/api/queue/commit", {"decisions": [
            {"id": pending[0], "action": "exclude_channel"},
            {"id": pending[1], "action": "exclude_channel"}]})
        first, second = data["results"]
        self.assertEqual((first["status"], first["closed"]), ("applied", 4))
        self.assertEqual(first["channel"], {"id": CHANNEL, "name": "dummy-general"})
        self.assertEqual(second["status"], "conflict")  # closed by the first decision
        for mid in pending + [assigned]:
            row = self.store.get_message(mid)
            self.assertEqual((row["state"], row["note"]), ("excluded", "human:console:channel"))
        self.assertEqual(self.store.get_message(done)["state"], "done")
        self.assertEqual(self.store.get_message(other)["state"], "human_pending")
        rows = self.store.list_channel_exclusions()
        self.assertEqual([(row["channel"], row["by"]) for row in rows], [(CHANNEL, "console")])
        self.assertEqual(self.get("/api/queue")["pending_ids"], [other])

    def test_router_honours_override_until_undo(self):
        mid = self.pending(1)
        message = dict(self.store.get_message(mid), text="[support] dummy")
        self.assertEqual(route(message, routing_config(self.config, self.store),
                               self.store.lookup_binding).lane, "support")
        data = self.post("/api/queue/actions", {"action": "exclude_channel", "ids": [mid]})
        self.assertEqual((data["applied"], data["results"][0]["closed"]), (1, 1))
        decision = route(message, routing_config(self.config, self.store), self.store.lookup_binding)
        self.assertEqual(decision.rule, "excluded")
        undo = self.post("/api/channel-exclusions/undo", {"channel": CHANNEL})
        self.assertEqual(undo, {"channel": CHANNEL, "removed": True})
        self.post("/api/channel-exclusions/undo", {"channel": CHANNEL}, status=404)
        self.post("/api/channel-exclusions/undo", {"channel": "bad id"}, status=422)
        self.assertEqual(route(message, routing_config(self.config, self.store),
                               self.store.lookup_binding).lane, "support")
        self.assertEqual(self.store.get_message(mid)["state"], "excluded")  # items stay closed

    def test_scope_tab_lists_and_exports_exclusions(self):
        self.assertEqual(self.get("/api/proposals")["channel_exclusions"], [])
        empty = self.get("/api/channel-exclusions/export")["yaml"]
        self.assertIn("excluded_channels:\n  - C0000000003\n", empty)
        self.assertIn("# No ledger channel exclusions.", empty)
        self.post("/api/queue/actions", {"action": "exclude_channel", "ids": [self.pending(1)]})
        listing = self.get("/api/proposals")["channel_exclusions"]
        self.assertEqual([(row["channel"], row["name"], row["by"], row["in_config"]) for row in listing],
                         [(CHANNEL, "dummy-general", "console", False)])
        text = self.get("/api/channel-exclusions/export")["yaml"]
        self.assertIn("excluded_channels:\n  - C0000000003\n  - C0000000002  # dummy-general, ledger\n", text)
        self.assertNotIn("# No ledger channel exclusions.", text)
        self.assertFalse(queries.channel_hint(self.store, self.config, self.store.get_message(
            self.pending(2))))

    def test_not_member_badge_follows_the_membership_table(self):
        mine = seed(self.store, ts(1), channel=OTHER, received=stamp(1))
        self.store.set_state(mine, "human_pending", note="none")
        theirs = self.pending(2)
        items = {item["id"]: item["not_member"] for item in self.get("/api/queue")["items"]}
        self.assertEqual(items, {mine: False, theirs: False})  # empty cache: no badge
        self.store.replace_membership([{"channel": OTHER, "kind": "public_channel"}], "2026-11-16T00:00:00+00:00")
        items = {item["id"]: item["not_member"] for item in self.get("/api/queue")["items"]}
        self.assertEqual(items, {mine: False, theirs: True})

    def test_page_strings(self):
        script = (STATIC / "console.js").read_text(encoding="utf-8")
        english = dictionary_keys(script, "en")
        self.assertEqual(english, dictionary_keys(script, "ko"))
        for key in ("excludeChannel", "notMember", "exclusionsTitle", "exportExclusions", "undo"):
            self.assertIn(key, english)
        korean = script[script.index("ko: Object.freeze({"):]
        self.assertIn('excludeChannel: "채널 제외"', korean)
        self.assertIn('notMember: "미가입 채널"', korean)
        self.assertIn('excludeChannel: "Exclude channel"', script[:script.index("ko: Object.freeze({")])
        self.assertIn('"exclude_channel"', script)


if __name__ == "__main__":
    unittest.main()
