"""P13 item 4: console Scope tab (proposals, decisions, learned scope export), dummy ids only."""
from dataclasses import replace
import unittest

import yaml

from tests.test_console_app import ConsoleCase, ts
from slack_dispatch.router.engine import route


class ProposalTabTests(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.config = replace(self.config, channel_defaults={"C0000000001": "support"},
                              people=dict(self.config.people, U0000000041="Dummy Partner"))
        self.client = self.make_client()

    def test_page_and_listing(self):
        page = self.get("/proposals")
        self.assertIn("/static/console.js", page.text)
        script = self.get("/static/console.js").text
        self.assertIn('"/proposals": "proposals"', script)
        store = self.store
        for _ in range(2):
            store.record_scope_evidence("channel", "C0000000002", "support", "reroute")
        store.record_scope_evidence("channel", "C0000000002", "content", "assign")
        store.record_scope_evidence("person", "U0000000041", "owner", "scope")
        store.add_scope_claim("owner", "channel", "C0000000009")
        store.record_scope_evidence("channel", "C0000000009", "owner", "scope")
        store.confirm_scope_claim("owner", "channel", "C0000000009", by="scope:auto")
        store.record_scope_evidence("dm", "D0000000041", "owner", "sent")
        store.decide_scope_proposal("dm", "D0000000041", "owner", "defer")
        data = self.get("/api/proposals")
        self.assertEqual(data["min_evidence"], 2)
        rows = {(row["kind"], row["key"], row["lane"]): row for row in data["proposals"]}
        self.assertEqual(set(rows), {("channel", "C0000000002", "support"), ("channel", "C0000000002", "content"),
                                     ("person", "U0000000041", "owner"), ("channel", "C0000000009", "owner")})
        general = rows[("channel", "C0000000002", "support")]
        self.assertEqual((general["name"], general["evidence_count"], general["status"], general["conflict"]),
                         ("dummy-general", 2, "open", True))
        self.assertEqual(sorted(item["lane"] for item in general["lanes"]), ["content", "support"])
        self.assertIsNone(general["current"])
        self.assertEqual(rows[("person", "U0000000041", "owner")]["name"], "Dummy Partner")
        auto = rows[("channel", "C0000000009", "owner")]
        self.assertEqual((auto["effective"], auto["current"]), (True, {"lane": "owner", "source": "scope:auto"}))
        self.assertEqual(data["learned"][0]["confirmed_by"], "scope:auto")

    def test_confirm_routes_without_yaml_edit_and_export(self):
        for _ in range(2):
            self.store.record_scope_evidence("channel", "C0000000002", "content", "reroute")
        self.store.record_scope_evidence("dm", "D0000000041", "owner", "sent")
        self.store.record_scope_evidence("person", "U0000000041", "owner", "scope")
        self.store.record_scope_evidence("channel", "C0000000001", "support", "scope")
        result = self.post("/api/proposals/actions",
                           {"action": "confirm", "kind": "channel", "key": "C0000000002", "lane": "content"})
        self.assertEqual(result["status"], "confirmed")
        for kind, key, lane in (("dm", "D0000000041", "owner"), ("person", "U0000000041", "owner"),
                                ("channel", "C0000000001", "support")):
            self.post("/api/proposals/actions", {"action": "confirm", "kind": kind, "key": key, "lane": lane})
        message = {"source": "slack", "channel": "C0000000002", "ts": "1.0", "user": "U0000000088",
                   "text": None}
        decision = route(message, self.config, self.store.lookup_binding)
        self.assertEqual((decision.lane, decision.rule), ("content", "scope_claim"))
        listing = self.get("/api/proposals")
        self.assertEqual(listing["proposals"], [])
        learned = {(row["kind"], row["key"]): row for row in listing["learned"]}
        self.assertTrue(learned[("channel", "C0000000001")]["in_config"])
        text = self.get("/api/proposals/export")["yaml"]
        fragment = yaml.safe_load(text)
        self.assertEqual(fragment["channel_defaults"], {"C0000000001": {"default": "support"},
                                                        "C0000000002": {"default": "content"}})
        self.assertEqual(fragment["dm_defaults"], {"D0000000041": {"default": "owner"}})
        self.assertIn("#   U0000000041: owner", text)
        self.assertIn("same as config", text)
        self.assertNotIn(chr(0x2014), text)

    def test_reject_defer_and_errors(self):
        self.store.record_scope_evidence("channel", "C0000000002", "support", "reroute")
        body = {"action": "reject", "kind": "channel", "key": "C0000000002", "lane": "support"}
        self.assertEqual(self.post("/api/proposals/actions", body)["status"], "rejected")
        self.post("/api/proposals/actions", body, status=409)
        self.post("/api/proposals/actions", dict(body, key="C0000000099"), status=404)
        self.post("/api/proposals/actions", dict(body, action="maybe"), status=422)
        self.post("/api/proposals/actions", dict(body, kind="thread"), status=422)
        self.store.record_scope_evidence("channel", "C0000000002", "ghost", "reroute")
        self.post("/api/proposals/actions", dict(body, action="confirm", lane="ghost"), status=400)
        self.assertEqual(self.post("/api/proposals/actions", dict(body, action="defer", lane="ghost"))["status"],
                         "deferred")
        empty = self.get("/api/proposals/export")["yaml"]
        self.assertIn("# No learned scope overrides.", empty)

    def test_queue_assign_from_console_learns_and_binds(self):
        mid = self.pending(10)
        result = self.post("/api/queue/actions", {"action": "assign", "ids": [mid], "lane": "support"})
        self.assertEqual(result["applied"], 1)
        self.assertEqual(self.store.lookup_binding("slack", "C0000000002", ts(10)), "support")
        row = self.store.get_scope_proposal("channel", "C0000000002", "support")
        self.assertEqual((row["evidence_count"], row["last_evidence"]), (1, "assign"))


if __name__ == "__main__":
    unittest.main()
