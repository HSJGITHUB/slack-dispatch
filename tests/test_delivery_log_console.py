"""P18 console Deliveries tab over HTTP: page, rows with receipts, summary (dummy ids only)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

from slack_dispatch.console import queries
from slack_dispatch.notify.base import DeliveryRecorder
from tests.test_console_app import NOW, ConsoleCase, LIVE_ID, stamp, ts
from tests.test_console_core import seed
from tests.test_console_staging import dictionary_keys

STATIC = Path(queries.__file__).with_name("static")


class DeliveriesTabTests(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.reply = seed(self.store, ts(20), thread=ts(10), received=stamp(20))
        self.root_id = seed(self.store, ts(10), received=stamp(10))
        for mid in (self.reply, self.root_id):
            self.store.set_route(mid, "support", "explicit_tag")

        def recorder(purpose, adapter, minutes_ago):
            return DeliveryRecorder(self.store, purpose=purpose, adapter=adapter,
                                    clock=lambda: NOW - minutes_ago * 60)

        recorder("lane", "session_message", 50).record(
            "support", [self.reply, self.root_id], "sent", kind="new",
            detail="delivered | {\"success\":true}", target_session_id=LIVE_ID,
            target_session_name="dummy-live", cost_usd=0.0123, duration_ms=4200,
            relay_session_id="9999eeee-0000-4000-8000-000000000009")
        recorder("lane", "session_message", 40).record("content", [self.root_id],
                                                       "skipped_paused", kind="reminder")
        recorder("human_queue", "dispatch_card", 30).record("human_queue", [self.reply], "sent",
                                                            kind="card", detail="cards written 1")
        recorder("lane", "session_message", 3000).record("support", [self.root_id], "failed",
                                                         kind="new", detail="OSError: errno 2")
        acked = datetime.fromtimestamp(NOW - 45 * 60, timezone.utc).isoformat()
        self.store._update(self.reply, {"ack_at": acked})

    def test_page_tab_and_strings(self):
        page = self.get("/deliveries")
        self.assertIn("text/html", page.headers["content-type"])
        self.assertIn('data-view="deliveries"', page.text)
        self.assertIn('id="view-deliveries"', page.text)
        script = (STATIC / "console.js").read_text(encoding="utf-8")
        self.assertIn('VIEWS["/deliveries"] = "deliveries";', script)
        self.assertIn('/api/deliveries/summary?hours=', script)
        en, ko = dictionary_keys(script, "en"), dictionary_keys(script, "ko")
        self.assertEqual(en, ko)
        for key in ("tabDeliveries", "deliveryColumns.receipt", "deliveryStatuses.skipped_ambiguous",
                    "relayOutcomes.not_delivered", "unconfirmedFor", "receivedAt"):
            self.assertIn(key, en)
        korean = script[script.index("  ko: Object.freeze({"):]
        for label in ('tabDeliveries: "전달"', '"시각"', '"대상 세션"', '"항목 수"', '"전송 결과"',
                      '"수신 확인"', 'noTargetSession: "세션 없음"'):
            self.assertIn(label, korean)
        self.assertIsNone(re.search("\u2014", script))

    def test_rows_newest_first_with_receipts_and_items(self):
        data = self.get("/api/deliveries")
        self.assertEqual((data["hours"], data["page"], data["page_size"], data["total"]), (48, 1, 50, 3))
        card, skipped, sent = data["rows"]
        self.assertEqual([card["kind"], skipped["kind"], sent["kind"]], ["card", "reminder", "new"])
        self.assertEqual(sent["target"], {"session_id": LIVE_ID, "short_id": LIVE_ID[:8],
                                          "name": "dummy-live"})
        self.assertEqual((sent["status"], sent["relay_outcome"], sent["item_count"]),
                         ("sent", "delivered", 2))
        self.assertEqual((sent["cost_usd"], sent["duration_ms"]), (0.0123, 4200))
        self.assertEqual(sent["time"], "00:10")
        self.assertEqual(sent["receipt"]["received_time"], "00:15")
        self.assertEqual(sent["receipt"]["latency_seconds"], 300)
        reply = next(item for item in sent["items"] if item["id"] == self.reply)
        self.assertEqual(reply["channel"], {"id": "C0000000002", "name": "dummy-general"})
        self.assertEqual(reply["author"], {"id": "U0000000088", "name": "Dummy Author"})
        self.assertTrue(reply["is_reply"])
        self.assertNotIn("body", reply)
        self.assertIsNone(skipped["target"])
        self.assertEqual(skipped["receipt"]["age_seconds"], None)
        self.assertEqual((card["purpose"], card["adapter"], card["receipt"]["received_at"]),
                         ("human_queue", "dispatch_card", None))

    def test_unconfirmed_row_has_age(self):
        self.store._update(self.reply, {"ack_at": None})
        sent = self.get("/api/deliveries?status=sent&purpose=lane")["rows"][0]
        self.assertEqual((sent["receipt"]["received_at"], sent["receipt"]["age_seconds"]),
                         (None, 3000))

    def test_filters_window_and_paging(self):
        self.assertEqual(self.get("/api/deliveries?hours=0")["total"], 4)
        self.assertEqual(self.get("/api/deliveries?hours=24&lane=content")["total"], 1)
        self.assertEqual(self.get("/api/deliveries?status=skipped")["rows"][0]["status"], "skipped_paused")
        self.assertEqual(self.get("/api/deliveries?purpose=human_queue")["total"], 1)
        self.assertEqual(self.get("/api/deliveries?lane=human_queue")["total"], 1)
        page = self.get("/api/deliveries?hours=0&page=2")
        self.assertEqual((page["page"], page["rows"]), (2, []))
        for query in ("lane=unknown", "purpose=other", "status=bogus", "hours=-1", "page=0"):
            self.get("/api/deliveries?" + query, 400 if "=" in query and query.split("=")[0] in
                     ("lane", "purpose", "status") else 422)

    def test_summary(self):
        self.assertEqual(self.get("/api/deliveries/summary"),
                         {"hours": 48, "total": 3, "sent": 2, "failed": 0, "timeout": 0, "skipped": 1,
                          "dry_run": 0, "not_delivered": 0, "unconfirmed": 0})
        everything = self.get("/api/deliveries/summary?hours=0")
        self.assertEqual((everything["total"], everything["failed"]), (4, 1))
        self.store._update(self.reply, {"ack_at": None})
        self.assertEqual(self.get("/api/deliveries/summary?hours=168")["unconfirmed"], 1)

    def test_empty_ledger_table(self):
        self.store._db.execute("DELETE FROM delivery_log")
        self.assertEqual(self.get("/api/deliveries")["rows"], [])
        self.assertEqual(self.get("/api/deliveries/summary")["total"], 0)

    def test_window_bound_uses_utc_text(self):
        bound = queries.delivery_since(NOW, 48)
        self.assertEqual(bound, (datetime.fromtimestamp(NOW, timezone.utc) - timedelta(hours=48))
                         .isoformat(timespec="microseconds"))
        self.assertIsNone(queries.delivery_since(NOW, 0))
