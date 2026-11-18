"""P32 demo seed: tools/demo/seed_demo.py writes a synthetic instance the console can open.

Every run writes into an OS temporary directory, never inside the repository.
"""
from collections import Counter
from contextlib import closing
import importlib.util
import io
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import unittest
import warnings

from slack_dispatch.config.loader import load_config
from slack_dispatch.console import queries
from slack_dispatch.notify.session_resolve import SessionResolver

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "demo" / "seed_demo.py"
spec = importlib.util.spec_from_file_location("seed_demo", SCRIPT)
seed_demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_demo)

NOW = int(time.time())
# Anything shaped like a Slack conversation or user id (a letter, then 7+ upper-case letters
# or digits with at least one digit) must use a demo prefix.
SLACK_ID = re.compile(r"\b[CDGUW](?=[A-Z0-9]*[0-9])[A-Z0-9]{7,}\b")
DEMO_PREFIXES = ("C0DEMO", "U0DEMO", "D0DEMO")


def temp_dir(case):
    temp = tempfile.TemporaryDirectory(prefix="dispatch-demo-seed-")
    case.addCleanup(temp.cleanup)
    return Path(temp.name)


def run_main(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    code = seed_demo.main(argv, stdout=stdout, stderr=stderr, now=NOW)
    return code, stdout.getvalue(), stderr.getvalue()


def snapshot(config_path):
    """Path-free content of a seeded ledger: message rows and delivery rows."""
    config = load_config(config_path)
    with closing(sqlite3.connect(config.ledger.path)) as db:
        messages = db.execute("SELECT id, channel, ts, thread_ts, author, state, lane, note, "
                              "attention, route_reason, body FROM messages ORDER BY id").fetchall()
        deliveries = db.execute("SELECT created_at, purpose, lane, kind, status, item_ids, "
                                "cost_usd FROM delivery_log ORDER BY id").fetchall()
    return messages, deliveries


class DemoSeedCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp = tempfile.TemporaryDirectory(prefix="dispatch-demo-seed-")
        cls.out = Path(cls._temp.name) / "demo"
        cls.summary = seed_demo.seed(cls.out, now=NOW)
        cls.config = load_config(cls.summary["config"])
        cls.store = queries.open_ledger(cls.config)
        cls.view = queries.View(cls.config)
        cls.resolver = SessionResolver(cls.config.sessions_dir,
                                       sessions_table=cls.config.console.sessions_table)
        cls.root = queries.inbox_root(cls.config)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls._temp.cleanup()

    def bound(self):
        return self.view.bind(self.store)


class SeedContentTests(DemoSeedCase):
    def test_config_loads_with_the_real_loader(self):
        config = self.config
        self.assertEqual(Path(self.summary["config"]), self.out.resolve() / "config.yaml")
        self.assertEqual([lane.id for lane in config.lanes],
                         ["operator", "support", "billing", "content", "data", "ops"])
        self.assertEqual(queries.direct_lane(config), "operator")
        self.assertEqual(config.mention_only_channels, ("C0DEMO0007",))
        self.assertTrue(config.ledger.store_body)
        self.assertEqual(config.sources, ())
        self.assertTrue(all(adapter.dry_run for adapter in config.notify_adapters
                            if adapter.kind == "session_message"))
        self.assertTrue(Path(config.ledger.path).is_file())
        self.assertEqual(config.console.host, "127.0.0.1")
        self.assertEqual(queries.operator_name(config), "Operator")

    def test_every_state_flag_and_table_is_covered(self):
        rows = self.store.list_messages(limit=1000)
        self.assertTrue(35 <= len(rows) <= 50, len(rows))
        states = Counter(row["state"] for row in rows)
        self.assertEqual(set(states), {"assigned", "acked", "done", "human_pending", "excluded"})
        self.assertTrue(3 <= states["human_pending"] <= 5)
        notes = {row["note"] for row in rows}
        self.assertTrue({"excluded:no_mention", "excluded:bot", "excluded:not_member",
                         "human:console:channel"} <= notes)
        self.assertEqual({row["attention"] for row in rows} - {None},
                         {"owner_mention", "always_human"})
        self.assertGreaterEqual(sum(row["is_thread_reply"] for row in rows), 5)
        self.assertGreaterEqual(len(self.store._rows("SELECT * FROM bindings")), 5)
        self.assertTrue(any(row["edited_at"] for row in rows))
        self.assertTrue(any(row["route_reason"] == "owner_post" for row in rows))
        claims = self.store.confirmed_scope_claims()
        self.assertEqual(len(claims), 2)
        statuses = Counter(row["status"] for row in self.store.list_scope_proposals())
        self.assertGreaterEqual(statuses["open"], 2)
        membership = self.store.list_membership()
        self.assertGreaterEqual(len(membership), 10)
        self.assertIn(0, {row["is_member"] for row in membership})
        directory = {row["user"]: row["name"] for row in self.store.list_user_directory()}
        self.assertIn("Ops Lead", directory.values())
        self.assertIn("Designer A", directory.values())
        deliveries = self.store.list_deliveries()
        kinds = {(row["purpose"], row["kind"]) for row in deliveries}
        self.assertTrue({("human_queue", "digest"), ("human_queue", "card"),
                         ("lane", "new")} <= kinds)
        receipts = self.store.delivery_receipts(deliveries, NOW)
        self.assertTrue(any(row["received_at"] for row in receipts))
        self.assertTrue(self.store.list_channel_exclusions())
        self.assertTrue(self.store.list_console_actions())

    def test_messages_fall_in_the_last_48_hours(self):
        for row in self.store.list_messages(limit=1000):
            ts = float(row["ts"])
            self.assertTrue(NOW - 48 * 3600 <= ts <= NOW, row["id"])
            for column in ("assigned_at", "ack_at", "done_at"):
                if row[column]:
                    moment = queries.seconds_since(row[column], NOW)
                    self.assertIsNotNone(moment)
                    self.assertLessEqual(moment, 48 * 3600, (row["id"], column))

    def test_only_demo_ids_are_written(self):
        texts = []
        for path in sorted(self.out.rglob("*")):
            if path.is_file() and not path.name.startswith("ledger.sqlite3"):
                texts.append(path.read_text(encoding="utf-8"))
        with closing(sqlite3.connect(self.config.ledger.path)) as db:
            texts.extend(db.iterdump())
        found = {match for text in texts for match in SLACK_ID.findall(text)}
        self.assertTrue(found)
        self.assertEqual({value for value in found if not value.startswith(DEMO_PREFIXES)},
                         set())

    def test_same_seed_same_content_and_another_seed_moves_times(self):
        again = seed_demo.seed(temp_dir(self) / "again", now=NOW)
        self.assertEqual(snapshot(again["config"]), snapshot(self.summary["config"]))
        other = seed_demo.seed(temp_dir(self) / "other", now=NOW, seed_value=7)
        first, second = snapshot(self.summary["config"]), snapshot(other["config"])
        self.assertEqual([row[1] for row in first[0]], [row[1] for row in second[0]])
        self.assertNotEqual([row[2] for row in first[0]], [row[2] for row in second[0]])


class ConsoleQueryTests(DemoSeedCase):
    """The read models behind each console tab return content for the demo data."""

    def test_queue_tab(self):
        page = queries.queue_page(self.store, self.config, self.bound(), NOW)
        self.assertEqual(page["counts"], {"undecided": 5, "direct": 3})
        self.assertEqual(page["total"], 8)
        kinds = {item["queue_kind"] for item in page["items"]}
        self.assertEqual(kinds, {"undecided", "direct"})
        self.assertTrue(all(item["body"] for item in page["items"]))
        names = {item["channel_name"] for item in page["items"]}
        self.assertIn("#general", names)
        self.assertTrue(any(item["is_reply"] and item["root"] for item in page["items"]))

    def test_lanes_tab(self):
        cards = queries.lane_cards(self.store, self.config, self.view, self.resolver, self.root,
                                   NOW)
        by_lane = {card["lane"]: card for card in cards}
        self.assertEqual(len(cards), 6)
        self.assertTrue(by_lane["support"]["identity"]["live"])
        self.assertEqual(by_lane["support"]["identity"]["title"], "Support desk")
        self.assertEqual(by_lane["content"]["identity"]["state"], "paused")
        self.assertEqual(by_lane["data"]["identity"]["state"], "not_live")
        self.assertFalse(by_lane["operator"]["listed"])
        self.assertTrue(any(card["open"] for card in cards))
        self.assertIsNotNone(by_lane["support"]["last_delivery"])
        self.assertIsNotNone(by_lane["support"]["last_reminder"])
        groups = queries.lane_inbox(self.store, self.config, self.bound(), self.root, "support")
        self.assertTrue(groups)
        self.assertTrue(any(group.get("owner_replied_at") for group in groups))

    def test_scope_tab(self):
        data = queries.proposals(self.store, self.config, self.bound())
        statuses = Counter(row["status"] for row in data["proposals"])
        self.assertEqual(statuses, Counter({"open": 2, "suggested": 1}))
        self.assertEqual(len(data["learned"]), 2)
        self.assertEqual([row["channel"] for row in data["channel_exclusions"]], ["C0DEMO0008"])
        self.assertIn("channel_defaults:", queries.export_yaml(self.store, self.config,
                                                                 self.bound()))

    def test_service_tab(self):
        status = queries.service_status(self.store, self.config, self.view, NOW)
        self.assertIsNotNone(status["heartbeat"])
        self.assertFalse(status["heartbeat"]["stale"])
        self.assertTrue(status["reconcile_passes"])
        self.assertTrue(status["warnings"])
        self.assertTrue(status["gaps"])
        self.assertFalse(status["wake_off"])
        self.assertTrue(status["adapters"])
        self.assertTrue(queries.recent_actions(self.store, self.bound(), NOW))

    def test_deliveries_tab(self):
        page = queries.deliveries_page(self.store, self.config, self.bound(), NOW)
        self.assertGreater(page["total"], 10)
        self.assertTrue(page["rows"])
        self.assertTrue(any(row["receipt"]["received_at"] for row in page["rows"]))
        self.assertTrue(all(item["preview"] for row in page["rows"] for item in row["items"]))
        summary = queries.deliveries_summary(self.store, NOW)
        self.assertEqual(summary["total"], page["total"])
        for key in ("sent", "skipped", "failed", "not_delivered", "unconfirmed"):
            self.assertGreater(summary[key], 0, key)

    def test_search(self):
        result = queries.search(self.store, self.bound(), "export")
        self.assertTrue(result["rows"])


class RefusalTests(unittest.TestCase):
    def test_refuses_a_directory_inside_the_repository(self):
        target = ROOT / "tests" / "tmp" / "demo-seed-refusal-check"
        tmp_existed = target.parent.exists()
        self.assertFalse(target.exists())
        code, stdout, stderr = run_main(["--out", str(target)])
        self.assertEqual(code, 2)
        self.assertIn("inside the repository", stderr)
        self.assertEqual(stdout, "")
        self.assertFalse(target.exists())
        self.assertEqual(target.parent.exists(), tmp_existed)
        code, _, stderr = run_main(["--out", str(ROOT)])
        self.assertEqual(code, 2)
        self.assertIn("inside the repository", stderr)
        code, _, stderr = run_main(["--out", str(ROOT.parent), "--force"])
        self.assertEqual(code, 2)
        self.assertIn("contains the repository", stderr)

    def test_refuses_a_non_empty_directory_unless_forced(self):
        out = temp_dir(self)
        (out / "notes.txt").write_text("keep me\n", encoding="utf-8")
        code, _, stderr = run_main(["--out", str(out)])
        self.assertEqual(code, 2)
        self.assertIn("not empty", stderr)
        self.assertEqual(sorted(path.name for path in out.iterdir()), ["notes.txt"])
        code, _, _ = run_main(["--out", str(out), "--force"])
        self.assertEqual(code, 0)
        self.assertEqual((out / "notes.txt").read_text(encoding="utf-8"), "keep me\n")
        load_config(out / "config.yaml")

    def test_force_moves_an_earlier_demo_aside(self):
        out = temp_dir(self)
        self.assertEqual(run_main(["--out", str(out)])[0], 0)
        self.assertEqual(run_main(["--out", str(out)])[0], 2)
        code, stdout, _ = run_main(["--out", str(out), "--force"])
        self.assertEqual(code, 0)
        self.assertIn("previous-1", stdout)
        self.assertTrue((out / "previous-1" / "state" / "ledger.sqlite3").is_file())
        self.assertTrue((out / "previous-1" / "config.yaml").is_file())
        self.assertEqual(snapshot(out / "config.yaml"),
                         snapshot(out / "previous-1" / "config.yaml"))
        store = queries.open_ledger(load_config(out / "config.yaml"))
        try:
            self.assertEqual(len(store.list_messages(state="human_pending")), 5)
        finally:
            store.close()

    def test_prints_the_console_command(self):
        out = temp_dir(self) / "demo"
        code, stdout, _ = run_main(["--out", str(out), "--port", "8899"])
        self.assertEqual(code, 0)
        config = out.resolve() / "config.yaml"
        self.assertIn(f'python -m slack_dispatch.console --config "{config}"', stdout)
        self.assertIn("127.0.0.1", stdout)
        self.assertIn("http://127.0.0.1:8899/", stdout)
        self.assertEqual(load_config(config).console.port, 8899)


try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - depends on the optional console extra
    TestClient = None


@unittest.skipIf(TestClient is None, "console HTTP tests need the optional console extra")
class ConsoleHttpTests(DemoSeedCase):
    ROUTES = ("/api/config", "/api/queue?offset=0", "/api/queue?kind=direct",
              "/api/queue/counts", "/api/lanes", "/api/lanes/support/inbox",
              "/api/lanes/data/inbox", "/api/service", "/api/actions?limit=20",
              "/api/proposals", "/api/proposals/export", "/api/channel-exclusions/export",
              "/api/deliveries?hours=48&page=1", "/api/deliveries/summary?hours=48",
              "/api/search?q=export")
    PAGES = ("/", "/lanes", "/service", "/proposals", "/deliveries")

    def test_every_tab_route_answers_200(self):
        from slack_dispatch.console.app import create_app
        client = TestClient(create_app(self.config, self.store, clock=lambda: NOW),
                            base_url="http://127.0.0.1")
        for path in self.PAGES:
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 200)
        for path in self.ROUTES:
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json())
        self.assertEqual(client.get("/api/queue").json()["counts"],
                         {"undecided": 5, "direct": 3})
        self.assertTrue(client.get("/api/deliveries").json()["rows"])


if __name__ == "__main__":
    unittest.main()
