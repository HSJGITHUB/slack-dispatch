"""P20 console over HTTP: names from the ledger, permalinks, and the Deliveries empty state."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import re
import unittest
import warnings

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - depends on the optional console extra
    raise unittest.SkipTest("console tests need the optional console extra") from None

from slack_dispatch.console import queries
from slack_dispatch.console.app import create_app
from slack_dispatch.inbox.files import append, record
from tests.test_console_app import NOW, ConsoleCase, stamp, ts
from tests.test_console_core import seed

DOMAIN = "dummy-workspace"
STATIC = Path(queries.__file__).with_name("static")
DIRECTORY_USER = "U0000000201"
PUBLIC = "C0000000211"
DM = "D0000000213"
SEEN = "2026-11-13T00:00:00+00:00"


class NamesAndLinksTests(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.config = replace(self.config, console=replace(self.config.console, workspace_domain=DOMAIN))
        self.client = TestClient(create_app(self.config, self.store, clock=lambda: NOW),
                                 base_url="http://127.0.0.1")
        self.store.put_user_directory(dict(user=DIRECTORY_USER, name="Dummy Directory Person"), SEEN)
        self.store.replace_membership([dict(channel=PUBLIC, kind="public_channel", name="dummy-public"),
                                       dict(channel=DM, kind="im", partner_user=DIRECTORY_USER)], SEEN)

    def link(self, channel, message_ts, thread=None):
        return queries.message_permalink(DOMAIN, channel, message_ts, thread)

    def test_config_flag(self):
        self.assertTrue(self.get("/api/config")["permalinks"])
        plain = TestClient(create_app(replace(self.config, console=replace(self.config.console,
                                                                           workspace_domain=None)),
                                      self.store, clock=lambda: NOW), base_url="http://127.0.0.1")
        self.assertFalse(plain.get("/api/config").json()["permalinks"])

    def test_queue_items_carry_names_and_permalinks(self):
        root = seed(self.store, ts(0), channel=DM, user=DIRECTORY_USER, text="DUMMY ROOT",
                    received=stamp(0))
        reply = seed(self.store, ts(10), channel=DM, thread=ts(0), user="U0000000299",
                     text="DUMMY REPLY", received=stamp(10))
        self.store.set_state(reply, "human_pending", note="none")
        item = self.get("/api/queue")["items"][0]
        self.assertEqual(item["id"], reply)
        self.assertEqual((item["channel_name"], item["author_name"]),
                         ("DM:Dummy Directory Person", "U0000000299"))
        self.assertEqual(item["channel"], {"id": DM, "name": "DM:Dummy Directory Person"})
        self.assertEqual(item["permalink"], self.link(DM, ts(10), ts(0)))
        self.assertEqual(item["root"]["permalink"], self.link(DM, ts(0)))
        self.assertEqual(item["root"]["author_name"], "Dummy Directory Person")
        self.assertEqual(item["root"]["id"], root)

    def test_lane_inbox_search_deliveries_and_actions(self):
        root = seed(self.store, ts(0), channel=PUBLIC, user=DIRECTORY_USER, received=stamp(0))
        reply = seed(self.store, ts(10), channel=PUBLIC, thread=ts(0), user=DIRECTORY_USER,
                     received=stamp(10))
        for mid in (root, reply):
            self.store.set_route(mid, "support", "explicit_tag")
        append(str(self.inbox), "support", [record(self.store.get_message(mid)) for mid in (root, reply)])
        group = self.get("/api/lanes/support/inbox")["groups"][0]
        self.assertEqual((group["root"]["channel_name"], group["root"]["author"]),
                         ("#dummy-public", "Dummy Directory Person"))
        self.assertEqual(group["root"]["permalink"], self.link(PUBLIC, ts(0)))
        self.assertEqual(group["items"][0]["permalink"], self.link(PUBLIC, ts(10), ts(0)))
        self.assertEqual(group["items"][0]["author"], "Dummy Directory Person")
        found = self.get("/api/search?q=" + reply)["rows"][0]
        self.assertEqual((found["channel_name"], found["author_name"], found["permalink"]),
                         ("#dummy-public", "Dummy Directory Person", self.link(PUBLIC, ts(10), ts(0))))
        self.store.add_delivery(dict(purpose="lane", lane="support", kind="new", status="sent",
                                     item_ids=[reply], created_at=stamp(20)))
        item = self.get("/api/deliveries?hours=0")["rows"][0]["items"][0]
        self.assertEqual(item["author"], {"id": DIRECTORY_USER, "name": "Dummy Directory Person"})
        self.assertEqual(item["permalink"], self.link(PUBLIC, ts(10), ts(0)))
        done = self.post("/api/lanes/support/actions", {"action": "done", "ids": [reply]})
        self.assertEqual(done["applied"], 1)
        action = self.get("/api/actions")["actions"][0]
        self.assertEqual(action["links"], [{"id": reply, "permalink": self.link(PUBLIC, ts(10), ts(0))}])

    def test_scope_tab_names(self):
        self.store.record_scope_evidence("dm", DM, "support", "assign")
        self.store.record_scope_evidence("person", DIRECTORY_USER, "support", "assign")
        names = {row["key"]: row["name"] for row in self.get("/api/proposals")["proposals"]}
        self.assertEqual(names, {DM: "DM:Dummy Directory Person", DIRECTORY_USER: "Dummy Directory Person"})

    def test_without_domain_no_permalinks(self):
        plain = TestClient(create_app(replace(self.config, console=replace(self.config.console,
                                                                           workspace_domain=None)),
                                      self.store, clock=lambda: NOW), base_url="http://127.0.0.1")
        mid = seed(self.store, ts(0), channel=PUBLIC, user=DIRECTORY_USER, received=stamp(0))
        self.store.set_state(mid, "human_pending", note="none")
        item = plain.get("/api/queue").json()["items"][0]
        self.assertIsNone(item["permalink"])
        self.assertEqual(item["author_name"], "Dummy Directory Person")

    def test_deliveries_empty_state_fields(self):
        data = self.get("/api/deliveries?hours=48")
        self.assertEqual(data["rows"], [])
        self.assertIsNotNone(data["log_started_at"])
        self.assertTrue(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", data["log_started_when"]))
        self.assertEqual(data["legacy_deliveries"], 0)
        start = datetime.fromtimestamp(NOW, timezone.utc).isoformat(timespec="microseconds")
        with self.store._transaction() as db:
            db.execute("UPDATE cursors SET cursor=? WHERE source='meta' AND channel='delivery_log_started_at'",
                       (start,))
            db.execute("""CREATE TABLE IF NOT EXISTS service_deliveries (
                source TEXT NOT NULL, channel TEXT NOT NULL, thread_ts TEXT NOT NULL,
                lane TEXT NOT NULL, delivered_at REAL NOT NULL,
                PRIMARY KEY (source, channel, thread_ts, lane))""")
            db.execute("INSERT INTO service_deliveries VALUES ('slack', ?, ?, 'support', ?)",
                       (PUBLIC, ts(0), NOW - 3600))
        data = self.get("/api/deliveries?hours=48")
        self.assertEqual((data["log_started_at"], data["legacy_deliveries"]), (start, 1))


class FrontEndTests(unittest.TestCase):
    def test_link_icon_and_labels(self):
        script = (STATIC / "console.js").read_text(encoding="utf-8")
        self.assertIn('target: "_blank", rel: "noopener noreferrer"', script)
        for text in ('openInSlack: "Slack에서 열기 (새 탭)"', 'groupDm: "그룹 DM"',
                     '"기록 시작: " + when', '"이전 방식 기록 " + count + "건 (상세 없음)"'):
            self.assertIn(text, script)
        for name in ("item.permalink", "item.root.permalink", "row.permalink", "root.permalink",
                     "row.links", "data.legacy_deliveries", "data.log_started_when"):
            self.assertIn(name, script)
        self.assertIn("a.permalink", (STATIC / "console.css").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
