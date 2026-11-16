"""P10 operator console over HTTP: FastAPI TestClient against a temp ledger (dummy ids only)."""
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
import warnings

try:
    with warnings.catch_warnings():
        # Newer Starlette warns that its httpx-based TestClient is deprecated.
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - depends on the optional console extra
    raise unittest.SkipTest("console tests need the optional console extra "
                            "(pip install -e .[console]: fastapi, uvicorn, httpx)") from None

from slack_dispatch.config.schema import Lane, PersonChannelRule
from slack_dispatch.console import queries
from slack_dispatch.console.app import create_app
from slack_dispatch.inbox.cli import main as cli_main
from slack_dispatch.inbox.files import append, record
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.runtime.heartbeat import HeartbeatWriter
from tests.test_console_core import open_store, seed
from tests.test_pipeline_integration import fixture_config

NOW = datetime(2026, 11, 13, 1, 0, tzinfo=timezone.utc).timestamp()
BASE = 1794528000  # 2026-11-13 00:00:00 UTC
LIVE_ID = "0000aaaa-0000-4000-8000-000000000001"
DEAD_ID = "2222cccc-0000-4000-8000-000000000004"


def ts(offset):
    return f"{BASE + offset}.000100"


def stamp(offset):
    return datetime.fromtimestamp(BASE + offset, timezone.utc).isoformat()


class ConsoleCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-console-app-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        base = fixture_config(self.root)
        sessions = self.root / "sessions"
        sessions.mkdir()
        (sessions / "one.json").write_text(json.dumps({"sessionId": LIVE_ID, "name": "dummy-live"}),
                                           encoding="utf-8")
        self.config = replace(
            base,
            ledger=replace(base.ledger, store_body=True),
            lanes=(Lane("owner", None, False), Lane("support", LIVE_ID[:8], False),
                   Lane("content", DEAD_ID, True)),
            person_channel_rules=(PersonChannelRule("U0000000088", "C0000000002", "content"),),
            sessions_dir=str(sessions),
            people={"U0000000088": "Dummy Author", "U0000000093": "Dummy Owner"},
            console=replace(base.console, direct_lane="owner",
                            channels={"C0000000002": "dummy-general"}))
        self.store = open_store(self.config.ledger.path)
        self.addCleanup(self.store.close)
        self.inbox = Path(self.config.inbox.root)
        self.client = self.make_client()

    def make_client(self, config=None, base_url="http://127.0.0.1"):
        app = create_app(config or self.config, self.store, clock=lambda: NOW)
        return TestClient(app, base_url=base_url)

    def pending(self, offset, *, thread=None, text=None, user="U0000000088", suggestion=None):
        mid = seed(self.store, ts(offset), thread=ts(thread) if thread is not None else None,
                   user=user, text=text, received=stamp(offset))
        self.store.set_state(mid, "human_pending", note="none")
        if suggestion:
            self.store.set_suggestion(mid, suggestion, "dummy reason", "v1")
        return mid

    def get(self, path, status=200, client=None):
        response = (client or self.client).get(path)
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json() if "json" in response.headers.get("content-type", "") else response

    def post(self, path, body, status=200):
        response = self.client.post(path, json=body)
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()


class PageAndGuardTests(ConsoleCase):
    def test_pages_static_and_no_store(self):
        for path in ("/", "/lanes", "/service"):
            response = self.get(path)
            self.assertIn("text/html", response.headers["content-type"])
            self.assertIn("/static/console.js", response.text)
        script = self.get("/static/console.js")
        self.assertIn("javascript", script.headers["content-type"])
        self.assertIn("const UI_TEXT = Object.freeze(", script.text)
        self.get("/static/console.css")
        self.get("/static/other.js", 404)
        self.get("/api/missing", 404)
        config = self.get("/api/config")
        self.assertEqual((config["lanes"], config["direct_lane"], config["page_size"]),
                         (["owner", "support", "content"], "owner", 50))

    def test_front_end_has_no_remote_assets(self):
        static = Path(queries.__file__).with_name("static")
        for name in ("console.html", "console.js", "console.css"):
            text = (static / name).read_text(encoding="utf-8")
            self.assertNotIn("http://", text)
            self.assertNotIn("https://", text)
            self.assertNotIn("\u2014", text)

    def test_host_and_origin_checks(self):
        self.get("/api/config", 403, client=self.make_client(base_url="http://dummy.example"))
        self.get("/api/config", client=self.make_client(base_url="http://localhost:8811"))
        response = self.client.post("/api/queue/actions", json={"action": "exclude", "ids": ["x"]},
                                    headers={"Origin": "http://dummy.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.headers["cache-control"], "no-store")
        same = self.client.post("/api/queue/actions", json={"action": "exclude", "ids": ["x"]},
                                headers={"Origin": "http://127.0.0.1:8811"})
        self.assertEqual(same.json()["results"][0]["error"], "not_found")

    def test_internal_errors_are_generic(self):
        self.store.close()
        with self.assertLogs("slack_dispatch.console.app", level="ERROR") as logs:
            response = self.client.get("/api/queue")
        self.assertEqual(logs.output, ["ERROR:slack_dispatch.console.app:"
                                       "Console request failed class=RuntimeError"])
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "Internal error"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.store.open()


class QueueTests(ConsoleCase):
    def seed_thread(self):
        root = seed(self.store, ts(0), text="R" * 350, user="U0000000093", received=stamp(0))
        self.store.set_route(root, "support", "channel_default")
        replies = [seed(self.store, ts(offset), thread=ts(0), text=f"DUMMY_REPLY_{offset}",
                        received=stamp(offset)) for offset in (10, 20, 30, 40, 50, 60)]
        self.store.set_route(replies[0], "support", "thread_inherit")
        item = self.pending(70, thread=0, text="DUMMY_ITEM full body " + "x" * 400,
                            suggestion="support")
        later = seed(self.store, ts(80), thread=ts(0), text="DUMMY_LATER", received=stamp(80))
        self.store.set_route(later, "support", "thread_inherit")
        return root, replies, item, later

    def test_listing_fields_and_ordering(self):
        root, replies, item, later = self.seed_thread()
        newest = self.pending(100, text="DUMMY_NEWEST")
        oldest = self.pending(-100, text=None)
        self.store.set_attention(oldest, "always_human")
        data = self.get("/api/queue")
        self.assertEqual(data["total"], 3)
        self.assertEqual([row["id"] for row in data["items"]], [newest, item, oldest])
        row = data["items"][1]
        self.assertEqual(row["when"], "2026-11-13 00:01")
        self.assertEqual(row["channel"], {"id": "C0000000002", "name": "dummy-general"})
        self.assertEqual(row["author"], {"id": "U0000000088", "name": "Dummy Author"})
        self.assertEqual(row["age_seconds"], 3600 - 70)
        self.assertEqual(row["body"], "DUMMY_ITEM full body " + "x" * 400)
        self.assertEqual(row["suggestion"], {"lane": "support", "reason": "dummy reason", "version": "v1"})
        self.assertEqual(row["pending_reason"], "none")
        self.assertTrue(row["is_reply"])
        self.assertEqual((row["root"]["id"], row["root"]["body"], row["root"]["truncated"]),
                         (root, "R" * 300, True))
        self.assertEqual(row["root"]["author"]["name"], "Dummy Owner")
        # Root plus the last five thread messages (excluding the item), oldest first.
        self.assertEqual([entry["id"] for entry in row["context"]], replies[2:] + [later])
        self.assertEqual([entry["after"] for entry in row["context"]], [False] * 4 + [True])
        self.assertEqual(row["thread_size"], 9)
        self.assertEqual(row["hints"], [{"lane": "content", "source": "rule", "channel": "C0000000002"},
                                        {"lane": "support", "source": "history", "count": 2}])
        top = data["items"][0]
        self.assertIsNone(top["root"])
        self.assertEqual(top["context"], [])
        last = data["items"][2]
        self.assertEqual((last["attention"], last["body"]), ("always_human", None))
        page = self.get("/api/queue?offset=1&limit=1")
        self.assertEqual(([row["id"] for row in page["items"]], page["total"]), ([item], 3))
        hints_off = self.make_client(replace(self.config, console=replace(
            self.config.console, lane_hint_people=False)))
        self.assertEqual(self.get("/api/queue", client=hints_off)["items"][1]["hints"], [])

    def test_queue_actions_match_cli_and_remove_rows(self):
        ids = [self.pending(offset, text=f"DUMMY_{offset}") for offset in (1, 2, 3, 4, 5, 6)]
        twin = open_store(self.root / "twin.sqlite3")
        self.addCleanup(twin.close)
        for offset in (1, 2, 3, 4, 5, 6):
            mid = seed(twin, ts(offset), text=f"DUMMY_{offset}", received=stamp(offset))
            twin.set_state(mid, "human_pending", note="none")
        twin_inbox = self.root / "twin-inbox"

        def cli(*args):
            code = cli_main(["--inbox-root", str(twin_inbox), *args], store=twin,
                            stdout=io.StringIO(), stderr=io.StringIO())
            self.assertEqual(code, 0)

        cli("assign", ids[0], "support")
        cli("assign", ids[1], "content", "--note", "dummy note")
        cli("assign", ids[2], "content", "--note", "dummy note")
        cli("assign", ids[3], "owner")
        result = self.post("/api/queue/actions", {"action": "assign", "ids": [ids[0]], "lane": "support"})
        self.assertEqual((result["applied"], result["failed"], result["lane"]), (1, 0, "support"))
        self.post("/api/queue/actions", {"action": "assign", "ids": ids[1:3], "lane": "content",
                                         "note": " dummy note "})
        self.post("/api/queue/actions", {"action": "direct", "ids": [ids[3]]})
        self.post("/api/queue/actions", {"action": "exclude", "ids": [ids[4]]})
        self.post("/api/queue/actions", {"action": "done", "ids": [ids[5]], "note": "dummy done"})
        for mid in ids[:4]:
            mine, theirs = self.store.get_message(mid), twin.get_message(mid)
            self.assertEqual((mine["state"], mine["lane"], mine["note"]),
                             (theirs["state"], theirs["lane"], theirs["note"]))
            self.assertEqual((mine["route_reason"], theirs["route_reason"]), ("human:console", "human:cli"))
        for lane in ("owner", "support", "content"):
            def lines(root):
                path = Path(root) / (lane + ".jsonl")
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                return [{key: value for key, value in row.items() if key != "delivered_at"} for row in rows]
            self.assertEqual(lines(self.inbox), lines(twin_inbox))
        self.assertEqual((self.store.get_message(ids[4])["state"], self.store.get_message(ids[4])["note"]),
                         ("excluded", "human:console"))
        self.assertEqual((self.store.get_message(ids[5])["state"], self.store.get_message(ids[5])["note"]),
                         ("done", "dummy done"))
        # P25: the item sent to the direct lane stays in the operator queue as a direct row.
        queue = self.get("/api/queue")
        self.assertEqual((queue["total"], queue["counts"]), (1, {"undecided": 0, "direct": 1}))
        self.assertEqual(self.get("/api/queue?kind=undecided")["total"], 0)
        again = self.post("/api/queue/actions", {"action": "assign", "ids": [ids[0], "slack:C1:1.0"],
                                                 "lane": "owner"})
        self.assertEqual([(row["ok"], row["error"]) for row in again["results"]],
                         [(False, "conflict"), (False, "not_found")])
        self.assertEqual(self.store.get_message(ids[0])["lane"], "support")

    def test_queue_action_validation(self):
        mid = self.pending(1)
        self.post("/api/queue/actions", {"action": "assign", "ids": [mid]}, 400)
        self.post("/api/queue/actions", {"action": "assign", "ids": [mid], "lane": "human_queue"}, 400)
        self.post("/api/queue/actions", {"action": "assign", "ids": [mid], "lane": "missing"}, 400)
        self.post("/api/queue/actions", {"action": "ack", "ids": [mid]}, 422)
        self.post("/api/queue/actions", {"action": "done", "ids": []}, 422)
        self.post("/api/queue/actions", {"action": "done", "ids": ["x"] * 201}, 422)
        no_direct = self.make_client(replace(self.config, console=replace(self.config.console,
                                                                          direct_lane="owner-direct")))
        response = no_direct.post("/api/queue/actions", json={"action": "direct", "ids": [mid]})
        self.assertEqual(response.status_code, 400)
        unregistered = replace(self.config, lanes=self.config.lanes + (Lane("extra", None, False),))
        response = self.make_client(unregistered).post(
            "/api/queue/actions", json={"action": "assign", "ids": [mid], "lane": "extra"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.store.get_message(mid)["state"], "human_pending")


class LaneTests(ConsoleCase):
    def assign(self, offset, lane="support", *, thread=None, text=None, received=None):
        mid = seed(self.store, ts(offset), thread=ts(thread) if thread is not None else None,
                   text=text, received=received or stamp(offset))
        self.store.set_route(mid, lane, "test")
        append(self.inbox, lane, [record(self.store.get_message(mid), "new")])
        return mid

    def test_lane_cards_counts_and_live_flag(self):
        first = self.assign(0)
        second = self.assign(10)
        third = self.assign(20, received="2026-11-12T10:00:00+00:00")  # received yesterday
        self.store.set_state(second, "acked")
        done = self.assign(30)
        self.store.set_state(done, "done")
        with self.store._transaction() as db:
            # Pin the action stamps (set_route and set_state use the real clock).
            for mid, offset in ((first, 0), (second, 10), (third, 20), (done, 30)):
                db.execute("UPDATE messages SET assigned_at=? WHERE id=?", (stamp(offset), mid))
            db.execute("UPDATE messages SET ack_at=? WHERE id=?", (stamp(15), second))
            db.execute("UPDATE messages SET done_at=? WHERE id=?", (stamp(35), done))
            db.execute("CREATE TABLE service_deliveries (source TEXT, channel TEXT, thread_ts TEXT, "
                       "lane TEXT, delivered_at REAL)")
            db.execute("INSERT INTO service_deliveries VALUES ('slack', 'C0000000002', ?, 'support', ?)",
                       (ts(0), BASE + 5))
            db.execute("INSERT INTO timer_events VALUES (?, 'unacknowledged', 1, ?)", (first, BASE + 50))
        cards = {card["lane"]: card for card in self.get("/api/lanes")["lanes"]}
        self.assertEqual(list(cards), ["owner", "support", "content"])
        support = cards["support"]
        self.assertEqual(support["session"], {"value": LIVE_ID[:8], "addressed_by": "id", "live": True,
                                              "state": "live", "target": "dummy-live"})
        self.assertFalse(support["paused"])
        self.assertTrue(support["registered"])
        self.assertEqual(support["today"], {"new": 3, "assigned": 4, "acked": 1, "done": 1})
        self.assertEqual(support["today"]["acked"], 1)
        self.assertEqual(support["today"]["done"], 1)
        self.assertEqual(support["open"], 3)
        self.assertEqual(support["oldest_unacked_seconds"], 3600)
        self.assertEqual(support["last_delivery"], "2026-11-13 00:00:05")
        self.assertEqual(support["last_reminder"], "2026-11-13 00:00:50")
        self.assertEqual(support["inbox_bytes"], (self.inbox / "support.jsonl").stat().st_size)
        content = cards["content"]
        self.assertEqual((content["session"]["live"], content["session"]["state"], content["paused"]),
                         (False, "not_live", True))
        owner = cards["owner"]
        self.assertEqual((owner["session"]["state"], owner["inbox_bytes"], owner["last_delivery"]),
                         ("no_session", None, None))

    def test_name_session_reports_live_file(self):
        config = replace(self.config, lanes=(Lane("owner", "dummy-live", False),
                                             Lane("support", "dummy-gone", False)))
        cards = {card["lane"]: card["session"] for card in
                 self.get("/api/lanes", client=self.make_client(config))["lanes"]}
        self.assertEqual((cards["owner"]["addressed_by"], cards["owner"]["live"]), ("name", True))
        self.assertFalse(cards["support"]["live"])

    def test_thread_view_matches_cli_grouping_and_lane_actions(self):
        root = self.assign(0, text="DUMMY_ROOT")
        reply = self.assign(10, thread=0, text="DUMMY_REPLY")
        other = self.assign(20, text="DUMMY_OTHER")
        owner_post = seed(self.store, ts(30), thread=ts(0), user="U0000000093", received=stamp(30))
        self.store.set_route(owner_post, None, "owner_post", state="done")
        out = io.StringIO()
        cli_main(["--inbox-root", str(self.inbox), "inbox", "support", "--thread", "--json"],
                 store=self.store, stdout=out, stderr=io.StringIO())
        expected = json.loads(out.getvalue())
        for cli_group in expected:  # the console applies the config `people` names
            for row in [cli_group["root"], *cli_group["items"]]:
                row["author"] = self.config.people.get(row["author_id"], row["author_id"])
        data = self.get("/api/lanes/support/inbox")
        self.assertEqual(data["lane"], "support")
        groups = data["groups"]
        self.assertEqual(len(groups), 2)
        for group, cli_group in zip(groups, expected):
            root_copy = dict(group["root"])
            root_copy.pop("pending")
            root_copy.pop("channel_name")
            self.assertEqual(root_copy.pop("when_full"), "2026-11-13 00:00")
            self.assertEqual(root_copy, cli_group["root"])
            self.assertEqual([{key: item[key] for key in cli_item} for item, cli_item
                              in zip(group["items"], cli_group["items"])], cli_group["items"])
        self.assertEqual(groups[0]["root"]["id"], root)
        self.assertTrue(groups[0]["root"]["pending"])
        self.assertEqual(groups[0]["root"]["channel_name"], "dummy-general")
        self.assertEqual([item["id"] for item in groups[0]["items"]], [reply])
        self.assertEqual(groups[0]["items"][0]["state"], "assigned")
        self.assertEqual(groups[0]["owner_replied_at"], ts(30))
        self.assertEqual(groups[0]["owner_replied_when"], "2026-11-13 00:00")
        self.assertEqual(groups[1]["root"]["id"], other)
        self.post("/api/lanes/support/actions", {"action": "ack", "ids": [reply]})
        self.assertEqual(self.store.get_message(reply)["state"], "acked")
        self.post("/api/lanes/support/actions", {"action": "done", "ids": [reply], "note": "dummy"})
        self.assertEqual((self.store.get_message(reply)["state"], self.store.get_message(reply)["note"]),
                         ("done", "dummy"))
        moved = self.post("/api/lanes/support/actions", {"action": "reroute", "ids": [other],
                                                         "lane": "content"})
        self.assertEqual(moved["applied"], 1)
        row = self.store.get_message(other)
        self.assertEqual((row["lane"], row["route_reason"], row["state"]),
                         ("content", "reroute:support", "assigned"))
        kinds = [json.loads(line)["kind"] for line in
                 (self.inbox / "content.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(kinds, ["reroute"])
        self.assertEqual([group["root"]["id"] for group in self.get("/api/lanes/support/inbox")["groups"]],
                         [root])
        stale = self.post("/api/lanes/support/actions", {"action": "ack", "ids": [other]})
        self.assertEqual(stale["results"][0]["error"], "conflict")
        closed = self.post("/api/lanes/support/actions", {"action": "ack", "ids": [reply]})
        self.assertEqual(closed["results"][0]["detail"], "state is done")
        self.assertEqual(self.store.get_message(reply)["state"], "done")
        self.post("/api/lanes/support/actions", {"action": "reroute", "ids": [root]}, 400)
        self.post("/api/lanes/human_queue/actions", {"action": "ack", "ids": [root]}, 400)
        self.get("/api/lanes/missing/inbox", 400)
        self.assertEqual(self.get("/api/lanes/owner/inbox")["groups"], [])


class ServiceAndSearchTests(ConsoleCase):
    def test_service_view_reads_heartbeat_log_gaps_and_flags(self):
        state = Path(self.config.service.state_dir)
        HeartbeatWriter(state / "heartbeat.json", clock=lambda: NOW - 20).write(
            status="running", version="0.1.0", counters={"events": 4, "failures": 0},
            started_at=stamp(0), last_event_at=stamp(60), last_catchup_at=stamp(30), gaps=1)
        logs = Path(self.config.service.log_dir)
        logs.mkdir(parents=True)
        lines = []
        for number in range(7):
            lines.append(f"2026-11-13 00:0{number}:00,000 INFO slack_dispatch.ingest.slack_reconcile "
                         f"Reconcile pass channels={number} roots=1 parents=0 replies=0 hydrated=0 "
                         "duplicates=0 prehandled=0 failures=0 thread_candidates=0 thread_skipped_age=0 "
                         "permanent_skipped=0; backfill delivered 2 (no wake)")
        for number in range(25):
            level = "ERROR" if number % 2 else "WARNING"
            lines.append(f"2026-11-13 00:10:{number:02d},000 {level} slack_dispatch.pipeline "
                         f"Pipeline exception operation=notify class=Dummy{number}")
        lines.append("2026-11-13 00:20:00,000 INFO slack_dispatch.service Service start")
        lines.append("not a log line")
        (logs / "service.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.store.record_gap("service", stamp(0), None, stamp(0), "start")
        state.mkdir(parents=True, exist_ok=True)
        (state / "wake.off").write_text("", encoding="utf-8")
        data = self.get("/api/service")
        heartbeat = data["heartbeat"]
        self.assertEqual((heartbeat["status"], heartbeat["ok"], heartbeat["stale"], heartbeat["age_seconds"]),
                         ("running", True, False, 20))
        self.assertEqual((heartbeat["started_at"], heartbeat["gaps"], heartbeat["counters"]["events"]),
                         (stamp(0), 1, 4))
        self.assertEqual([row["counts"]["channels"] for row in data["reconcile_passes"]], [6, 5, 4, 3, 2])
        self.assertEqual(data["reconcile_passes"][0]["counts"]["backfill"], 2)
        self.assertEqual(len(data["warnings"]), 20)
        self.assertEqual(data["warnings"][0]["message"], "Pipeline exception operation=notify class=Dummy24")
        self.assertEqual({row["level"] for row in data["warnings"]}, {"WARNING", "ERROR"})
        self.assertEqual([row["note"] for row in data["gaps"]], ["start"])
        self.assertTrue(data["wake_off"])
        self.assertEqual(data["human_queue"]["adapters"],
                         [{"index": 2, "kind": "file_drop", "purpose": "human_queue", "dry_run": False}])
        self.assertEqual(len(data["adapters"]), 3)
        stale = self.make_client()
        (state / "wake.off").unlink()
        HeartbeatWriter(state / "heartbeat.json", clock=lambda: NOW - 600).write(force=True)
        data = self.get("/api/service", client=stale)
        self.assertTrue(data["heartbeat"]["stale"])
        self.assertFalse(data["wake_off"])

    def test_service_view_without_files(self):
        data = self.get("/api/service")
        self.assertIsNone(data["heartbeat"])
        self.assertEqual((data["reconcile_passes"], data["warnings"], data["gaps"]), ([], [], []))

    def test_search(self):
        first = self.pending(1, text="DUMMY needle 50% off")
        second = seed(self.store, ts(2), text="DUMMY other", user="U0000000077", received=stamp(2))
        self.store.set_route(second, "support", "channel_default")
        elsewhere = seed(self.store, ts(3), channel="C0000000009", text="plain", received=stamp(3))
        by_body = self.get("/api/search?q=needle")
        self.assertEqual([row["id"] for row in by_body["rows"]], [first])
        self.assertEqual((by_body["rows"][0]["state"], by_body["rows"][0]["snippet"]),
                         ("human_pending", "DUMMY needle 50% off"))
        self.assertEqual([row["id"] for row in self.get("/api/search?q=U0000000077")["rows"]], [second])
        self.assertEqual(self.get("/api/search?q=U0000000077")["rows"][0]["lane"], "support")
        self.assertEqual([row["id"] for row in self.get("/api/search?q=C0000000002")["rows"]],
                         [second, first])
        self.assertEqual([row["id"] for row in self.get("/api/search?q=" + ts(3))["rows"]], [elsewhere])
        self.assertEqual(self.get("/api/search?q=%25%20o")["rows"][0]["id"], first)
        self.assertEqual(self.get("/api/search?q=xx_nothing")["rows"], [])
        self.get("/api/search?q=a", 400)
        self.get("/api/search", 400)
        view = queries.View(self.config)
        limited = queries.search(self.store, view, "C0000000002", limit=1)
        self.assertEqual((len(limited["rows"]), limited["truncated"]), (1, True))
        self.assertEqual(queries.SEARCH_LIMIT, 200)

    def test_reads_log_nothing(self):
        self.pending(1, text="DUMMY_SECRET_BODY")
        with self.assertNoLogs("slack_dispatch", level="DEBUG"):
            self.get("/api/queue")
            self.get("/api/search?q=SECRET")
            self.get("/api/service")


class LedgerOpenTests(unittest.TestCase):
    def test_open_ledger_sets_busy_timeout_and_migrates(self):
        with tempfile.TemporaryDirectory() as temp:
            config = fixture_config(Path(temp))
            seeded = LedgerStore(config.ledger.path)
            seeded.open()
            seeded.migrate()
            seeded.close()
            store = queries.open_ledger(config)
            try:
                self.assertEqual(store._rows("PRAGMA busy_timeout")[0]["timeout"], 15000)
                self.assertEqual(store._rows("SELECT version FROM schema_version")[0]["version"], 2)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
