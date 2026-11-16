"""P19 console undo: action journal, undo API, config and CLI (dummy ids only)."""
from contextlib import closing, redirect_stderr
from dataclasses import replace
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from slack_dispatch.config.loader import ConfigError, load_config, parse_config
from slack_dispatch.console import actions
from slack_dispatch.console.app import create_app
from slack_dispatch.ledger.store import SNAPSHOT_FIELDS, LedgerStore
from tests.test_console_app import NOW, ConsoleCase, TestClient, stamp, ts
from tests.test_console_core import sample_data, seed
from tests.test_pipeline_integration import SAMPLE


def fields(store, mid):
    row = store.get_message(mid)
    return {name: row[name] for name in SNAPSHOT_FIELDS}


class UndoCase(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.now = NOW
        self.client = self.make_client()

    def make_client(self, config=None, base_url="http://127.0.0.1"):
        app = create_app(config or self.config, self.store, clock=lambda: self.now)
        return TestClient(app, base_url=base_url)

    def journal(self):
        return self.store.list_console_actions(100)

    def lane_item(self, offset, lane="support", channel="C0000000002"):
        mid = seed(self.store, ts(offset), channel=channel, received=stamp(offset))
        self.store.set_route(mid, lane, "test")
        return mid

    def undo(self, action_id, status=200):
        return self.post(f"/api/undo/{action_id}", {}, status)

    def wakes(self, mid):
        return self.store._rows("SELECT * FROM wake_requests WHERE message_id=? ORDER BY id", (mid,))


class JournalTests(UndoCase):
    def test_apply_now_actions_write_one_row_each(self):
        ids = [self.pending(offset) for offset in (1, 2, 3, 4)]
        befores = {mid: fields(self.store, mid) for mid in ids}
        cases = [("assign", ids[0], {"lane": "content"}), ("direct", ids[1], {}),
                 ("exclude", ids[2], {}), ("done", ids[3], {"note": "dummy"})]
        for action, mid, extra in cases:
            data = self.post("/api/queue/actions", dict(action=action, ids=[mid], **extra))
            undo = data["undo"]
            self.assertEqual((undo["action"], undo["count"], undo["seconds_left"]), (action, 1, 30))
            row = self.store.get_console_action(undo["action_id"])
            self.assertEqual((row["action"], json.loads(row["ids"]), row["by"]), (action, [mid], "console"))
            before, after = json.loads(row["before"]), json.loads(row["after"])
            # fix1: assign and direct also record the thread binding they changed.
            self.assertEqual("binding" in before[0], action in ("assign", "direct"))
            self.assertEqual({k: v for k, v in before[0].items() if k != "binding"},
                             dict(befores[mid], id=mid))
            self.assertEqual({k: v for k, v in after[0].items() if k != "binding"},
                             dict(fields(self.store, mid), id=mid))
            self.assertIsNone(row["undone_at"])
            self.assertEqual(row["at"], actions.stamp_at(NOW))
            self.assertEqual(row["undo_until"], actions.stamp_at(NOW + 30))
        meta = json.loads(self.journal()[-1]["meta"])  # oldest row: the assign
        self.assertEqual(meta["lane"], "content")
        self.assertEqual(meta["wake_requests"], [row["id"] for row in self.wakes(ids[0])])
        self.assertEqual(len(self.journal()), 4)

    def test_no_row_when_nothing_changed(self):
        mid = self.lane_item(1)
        data = self.post("/api/queue/actions", {"action": "done", "ids": [mid, "slack:C1:1.0"]})
        self.assertEqual((data["applied"], data["undo"]), (0, None))
        data = self.post("/api/queue/commit", {"decisions": [{"id": mid, "action": "done"}]})
        self.assertIsNone(data["undo"])
        self.assertEqual(self.journal(), [])

    def test_commit_is_one_row(self):
        ids = [self.pending(offset) for offset in (1, 2, 3)]
        handled = self.pending(4)
        self.store.set_route(handled, "support", "test")
        data = self.post("/api/queue/commit", {"decisions": [
            {"id": ids[0], "action": "assign", "lane": "support"},
            {"id": ids[1], "action": "done", "note": "dummy"},
            {"id": handled, "action": "exclude"},
            {"id": ids[2], "action": "direct"}]})
        self.assertEqual(data["undo"]["count"], 3)
        rows = self.journal()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["action"], json.loads(rows[0]["ids"])), ("commit", ids))
        meta = json.loads(rows[0]["meta"])
        self.assertEqual(sorted(meta["wake_requests"]),
                         sorted(row["id"] for mid in (ids[0], ids[2]) for row in self.wakes(mid)))

    def test_lane_actions_are_journaled(self):
        ids = [self.lane_item(offset) for offset in (1, 2, 3)]
        for action, mid, extra in (("ack", ids[0], {}), ("done", ids[1], {}),
                                   ("reroute", ids[2], {"lane": "content"})):
            data = self.post("/api/lanes/support/actions", dict(action=action, ids=[mid], **extra))
            self.assertEqual(data["undo"]["action"], "lane_" + action)
        rows = self.journal()
        self.assertEqual([row["action"] for row in rows], ["lane_reroute", "lane_done", "lane_ack"])
        self.assertEqual(json.loads(rows[0]["meta"])["lane"], "content")
        self.assertEqual(json.loads(rows[1]["meta"])["lane"], "support")

    def test_exclude_channel_journals_every_closed_item(self):
        clicked = self.pending(1)
        open_lane = self.lane_item(2)
        self.store.set_state(open_lane, "acked")
        closed = self.lane_item(3)
        self.store.set_state(closed, "done")
        other = self.lane_item(4, channel="C0000000009")
        data = self.post("/api/queue/actions", {"action": "exclude_channel", "ids": [clicked]})
        self.assertEqual(data["results"][0]["closed"], 2)
        row = self.store.get_console_action(data["undo"]["action_id"])
        self.assertEqual(sorted(json.loads(row["ids"])), sorted([clicked, open_lane]))
        exclusion = self.store.list_channel_exclusions()[0]
        self.assertEqual(json.loads(row["meta"])["channel_exclusions"], [exclusion["id"]])
        self.assertNotIn(other, json.loads(row["ids"]))

    def test_proposal_decisions_are_not_journaled(self):
        self.store.record_scope_evidence("channel", "C0000000002", "support", "assign")
        self.post("/api/proposals/actions", {"action": "defer", "kind": "channel",
                                             "key": "C0000000002", "lane": "support"})
        self.assertEqual(self.journal(), [])


class UndoTests(UndoCase):
    def test_undo_restores_fields_exactly(self):
        ids = [self.pending(offset, suggestion="support") for offset in (1, 2)]
        lane_mid = self.lane_item(3)
        self.store.set_state(lane_mid, "acked", note="dummy ack")
        before = {mid: fields(self.store, mid) for mid in ids + [lane_mid]}
        commit = self.post("/api/queue/commit", {"decisions": [
            {"id": ids[0], "action": "assign", "lane": "content", "note": "dummy note"},
            {"id": ids[1], "action": "done"}]})["undo"]
        lane = self.post("/api/lanes/support/actions", {"action": "done", "ids": [lane_mid]})["undo"]
        result = self.undo(commit["action_id"])
        self.assertEqual(result["counts"]["restored"], 2)
        self.assertEqual([row["status"] for row in result["results"]], ["restored", "restored"])
        self.undo(lane["action_id"])
        for mid in ids + [lane_mid]:
            self.assertEqual(fields(self.store, mid), before[mid])
        stored = self.store.get_console_action(commit["action_id"])
        self.assertEqual(stored["undone_at"], actions.stamp_at(NOW))
        self.assertEqual(json.loads(stored["result"])["counts"]["restored"], 2)
        queue = self.get("/api/queue")
        self.assertEqual(sorted(queue["pending_ids"]), sorted(ids))

    def test_conflict_when_row_changed_meanwhile(self):
        first, second = self.pending(1), self.pending(2)
        info = self.post("/api/queue/actions", {"action": "assign", "ids": [first, second],
                                                "lane": "support"})["undo"]
        self.store.set_state(first, "acked")  # the lane picked it up meanwhile
        changed = fields(self.store, first)
        result = self.undo(info["action_id"])
        self.assertEqual([(row["id"], row["status"]) for row in result["results"]],
                         [(first, "conflict"), (second, "restored")])
        self.assertEqual(result["results"][0]["detail"], "now acked in support")
        self.assertEqual(result["counts"], {"restored": 1, "conflict": 1, "not_found": 0, "wake_sent": 0,
                                            "wake_removed": 1, "channel_exclusions_ended": 0,
                                            "binding_restored": 1, "binding_conflict": 0})
        self.assertEqual([row["binding"] for row in result["results"]], [None, "restored"])
        self.assertFalse(result["nothing_restored"])
        self.assertEqual(fields(self.store, first), changed)
        self.assertEqual(len(self.wakes(first)), 1)  # left alone: the item stays with the lane
        self.assertEqual(self.wakes(second), [])
        self.assertEqual(self.store.get_message(second)["state"], "human_pending")

    def test_window_expiry_and_repeat(self):
        mid = self.pending(1)
        info = self.post("/api/queue/actions", {"action": "done", "ids": [mid]})["undo"]
        self.now = NOW + 31
        self.assertEqual(self.undo(info["action_id"], 409)["detail"], "Undo window expired")
        self.assertEqual(self.store.get_message(mid)["state"], "done")
        self.now = NOW + 30  # the last second of the window still counts
        self.undo(info["action_id"])
        self.assertEqual(self.undo(info["action_id"], 409)["detail"], "Already undone")
        self.assertEqual(self.undo(999, 404)["detail"], "Action not found")
        self.post("/api/undo/abc", {}, 422)

    def test_custom_window(self):
        client = self.make_client(replace(self.config, console=replace(self.config.console,
                                                                       undo_seconds=10)))
        mid = self.pending(1)
        info = client.post("/api/queue/actions", json={"action": "done", "ids": [mid]}).json()["undo"]
        self.assertEqual(info["seconds_left"], 10)
        self.now = NOW + 11
        self.assertEqual(client.post(f"/api/undo/{info['action_id']}", json={}).status_code, 409)
        self.assertEqual(client.get("/api/config").json()["undo_seconds"], 10)

    def test_wake_requests_removed_or_reported_sent(self):
        first, second = self.pending(1), self.pending(2)
        info = self.post("/api/queue/commit", {"decisions": [
            {"id": first, "action": "assign", "lane": "support"},
            {"id": second, "action": "assign", "lane": "support"}]})["undo"]
        sent = self.wakes(first)[0]["id"]
        self.store.consume_wake_requests([sent])  # the service already offered this wake
        foreign = self.store.request_wake("support", second, "cli")  # not written by the action
        result = self.undo(info["action_id"])
        rows = {row["id"]: row for row in result["results"]}
        self.assertEqual((rows[first]["wake_sent"], rows[first]["wake_removed"]), (True, 0))
        self.assertEqual((rows[second]["wake_sent"], rows[second]["wake_removed"]), (False, 1))
        self.assertEqual((result["counts"]["wake_sent"], result["counts"]["wake_removed"]), (1, 1))
        self.assertEqual([row["id"] for row in self.wakes(first)], [sent])
        self.assertEqual([row["id"] for row in self.wakes(second)], [foreign])

    def test_reroute_undo_removes_its_wake(self):
        mid = self.lane_item(1)
        info = self.post("/api/lanes/support/actions", {"action": "reroute", "ids": [mid],
                                                        "lane": "content"})["undo"]
        self.assertEqual(len(self.wakes(mid)), 1)
        result = self.undo(info["action_id"])
        self.assertEqual(result["counts"]["wake_removed"], 1)
        self.assertEqual(self.wakes(mid), [])
        self.assertEqual((self.store.get_message(mid)["lane"], self.store.get_message(mid)["route_reason"]),
                         ("support", "test"))

    def test_channel_exclusion_undo(self):
        clicked = self.pending(1)
        lane_mid = self.lane_item(2)
        before = {mid: fields(self.store, mid) for mid in (clicked, lane_mid)}
        info = self.post("/api/queue/actions", {"action": "exclude_channel", "ids": [clicked]})["undo"]
        self.assertEqual(self.store.channel_exclusion_ids(), {"C0000000002"})
        result = self.undo(info["action_id"])
        self.assertEqual((result["counts"]["restored"], result["counts"]["channel_exclusions_ended"]), (2, 1))
        self.assertEqual(self.store.channel_exclusion_ids(), set())
        for mid in (clicked, lane_mid):
            self.assertEqual(fields(self.store, mid), before[mid])

    def test_channel_exclusion_undo_keeps_an_earlier_exclusion(self):
        self.store.add_channel_exclusion("C0000000002", "console")  # already excluded before
        clicked = self.pending(1)
        info = self.post("/api/queue/actions", {"action": "exclude_channel", "ids": [clicked]})["undo"]
        result = self.undo(info["action_id"])
        self.assertEqual(result["counts"]["channel_exclusions_ended"], 0)
        self.assertEqual(self.store.channel_exclusion_ids(), {"C0000000002"})
        self.assertEqual(self.store.get_message(clicked)["state"], "human_pending")

    def test_channel_exclusion_already_ended_by_scope_undo(self):
        clicked = self.pending(1)
        info = self.post("/api/queue/actions", {"action": "exclude_channel", "ids": [clicked]})["undo"]
        self.post("/api/channel-exclusions/undo", {"channel": "C0000000002"})
        result = self.undo(info["action_id"])
        self.assertEqual((result["counts"]["restored"], result["counts"]["channel_exclusions_ended"]), (1, 0))

    def test_undo_last(self):
        self.assertEqual(self.post("/api/undo/last", {}, 404)["detail"], "Nothing to undo")
        first, second = self.pending(1), self.pending(2)
        self.post("/api/queue/actions", {"action": "done", "ids": [first]})
        newest = self.post("/api/queue/actions", {"action": "exclude", "ids": [second]})["undo"]
        result = self.post("/api/undo/last", {})
        self.assertEqual(result["action_id"], newest["action_id"])
        self.assertEqual(self.store.get_message(second)["state"], "human_pending")
        self.assertEqual(self.store.get_message(first)["state"], "done")
        again = self.post("/api/undo/last", {})  # the older one is next
        self.assertEqual(again["action"], "done")
        self.assertEqual(self.store.get_message(first)["state"], "human_pending")
        self.post("/api/undo/last", {}, 404)
        third = self.pending(3)
        self.post("/api/queue/actions", {"action": "done", "ids": [third]})
        self.now = NOW + 60
        self.post("/api/undo/last", {}, 404)

    def test_undo_needs_a_json_body(self):
        mid = self.pending(1)
        info = self.post("/api/queue/actions", {"action": "done", "ids": [mid]})["undo"]
        response = self.client.post(f"/api/undo/{info['action_id']}", data={"x": "1"})
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/api/undo/last",
                                    headers={"Origin": "http://dummy.example"}, json={})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.get_message(mid)["state"], "done")


class ActionsApiTests(UndoCase):
    def test_recent_actions_listing(self):
        self.assertEqual(self.get("/api/actions"), {"undo_seconds": 30, "actions": []})
        ids = [self.pending(offset) for offset in (1, 2)]
        first = self.post("/api/queue/actions", {"action": "assign", "ids": [ids[0]],
                                                 "lane": "support"})["undo"]
        self.now = NOW + 12
        second = self.post("/api/queue/commit", {"decisions": [{"id": ids[1], "action": "done"}]})["undo"]
        self.undo(second["action_id"])
        self.now = NOW + 20
        rows = self.get("/api/actions?limit=20")["actions"]
        self.assertEqual([row["id"] for row in rows], [second["action_id"], first["action_id"]])
        newest, oldest = rows
        self.assertEqual((newest["action"], newest["count"], newest["by"], newest["undoable"],
                          newest["seconds_left"], newest["result"]["counts"]["restored"]),
                         ("commit", 1, "console", False, 0, 1))
        self.assertEqual((oldest["action"], oldest["lane"], oldest["ids"], oldest["undoable"],
                          oldest["seconds_left"], oldest["result"], oldest["undone_at"]),
                         ("assign", "support", [ids[0]], True, 10, None, None))
        self.assertEqual(oldest["time"], "01:00")
        self.now = NOW + 31
        self.assertFalse(self.get("/api/actions")["actions"][1]["undoable"])
        self.assertEqual(len(self.get("/api/actions?limit=1")["actions"]), 1)
        self.get("/api/actions?limit=0", 422)
        self.get("/api/actions?limit=201", 422)

    def test_front_end_strings(self):
        from tests.test_console_staging import STATIC
        script = (STATIC / "console.js").read_text(encoding="utf-8")
        korean = script[script.index("ko: Object.freeze({"):]
        for text in ('"되돌리기 (" + seconds + "초)"', '"복구 " + c.restored + "건"',
                     '"충돌 " + c.conflict + "건"', '"깨우기는 이미 나감 " + c.wake_sent + "건"',
                     'laneHandled: "처리됨"', 'recentActions: "최근 작업"', 'undoExpired: "만료"',
                     'undoUndone: "되돌림"'):
            self.assertIn(text, korean)
        for text in ('"/api/undo/"', '"/api/actions?limit=20"', 'event.key !== "z"'):
            self.assertIn(text, script)
        page = (STATIC / "console.html").read_text(encoding="utf-8")
        self.assertIn('id="toast"', page)


class UndoConfigTests(unittest.TestCase):
    def test_default_and_minimum(self):
        self.assertEqual(load_config(SAMPLE).console.undo_seconds, 30)
        data = sample_data()
        del data["console"]["undo_seconds"]
        self.assertEqual(parse_config(data).console.undo_seconds, 30)
        del data["console"]
        self.assertEqual(parse_config(data).console.undo_seconds, 30)
        data = sample_data()
        for value in (10, 45, 600):
            data["console"]["undo_seconds"] = value
            self.assertEqual(parse_config(data).console.undo_seconds, value)
        for value in (9, 0, -5, "30", 30.0, True, None):
            with self.subTest(value=value):
                data["console"]["undo_seconds"] = value
                with self.assertRaises(ConfigError):
                    parse_config(data)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-undo-migrate-")
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "ledger.sqlite3"

    def columns(self):
        with closing(sqlite3.connect(self.path)) as db:
            return [row[1] for row in db.execute("PRAGMA table_info(console_actions)")]

    def test_migration_is_additive_and_idempotent(self):
        store = LedgerStore(self.path)
        store.open()
        store.migrate()
        mid = seed(store, ts(1))
        action_id = store.add_console_action(at="2026-11-13T00:00:00+00:00", action="done", ids=[mid],
                                             before=[], after=[], by="console",
                                             undo_until="2026-11-13T00:00:30+00:00")
        store.migrate()
        store.migrate()
        self.assertEqual(store.get_console_action(action_id)["action"], "done")
        self.assertEqual(store.get_message(mid)["state"], "new")
        self.assertEqual(store._rows("SELECT version FROM schema_version"), [{"version": 2}])
        store.close()
        self.assertEqual(self.columns(), ["id", "at", "action", "ids", "before", "after", "by",
                                          "undo_until", "undone_at", "result", "meta"])

    def test_older_ledger_and_older_table_shape(self):
        store = LedgerStore(self.path)
        store.open()
        store.migrate()
        store.close()
        with closing(sqlite3.connect(self.path)) as db, db:  # a pre-P19 ledger, then a partial table
            db.execute("DROP TABLE console_actions")
            db.execute("CREATE TABLE console_actions (id INTEGER PRIMARY KEY, at TEXT, action TEXT, "
                       "ids TEXT, before TEXT, after TEXT, by TEXT, undo_until TEXT)")
            db.execute("INSERT INTO console_actions (at, action, ids) VALUES ('x', 'done', '[]')")
        store = LedgerStore(self.path)
        store.open()
        store.migrate()
        self.assertEqual(self.columns()[-3:], ["undone_at", "result", "meta"])
        self.assertEqual(store.list_console_actions()[0]["action"], "done")
        store.close()


class UndoCliTests(UndoCase):
    def run_cli(self, *argv, clock=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(io.StringIO()):  # argparse usage errors
            code = actions.undo_main(list(argv), stdout=out, stderr=err,
                                     clock=clock or (lambda: self.now))
        return code, out.getvalue(), err.getvalue()

    def test_undo_last_and_by_id(self):
        path = str(self.config.ledger.path)
        self.assertEqual(self.run_cli("--ledger", path, "undo", "--last")[0], 3)
        first, second = self.pending(1), self.pending(2)
        info = self.post("/api/queue/actions", {"action": "done", "ids": [first]})["undo"]
        self.store.set_route(second, "support", "test")
        self.post("/api/lanes/support/actions", {"action": "done", "ids": [second]})
        code, out, err = self.run_cli("undo", "--last", "--ledger", path)
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("undone action 2 (lane_done): restored 1, conflict 0"))
        self.assertEqual(self.store.get_message(second)["state"], "assigned")
        code, out, _ = self.run_cli("--ledger", path, "undo", str(info["action_id"]), "--json")
        result = json.loads(out)
        self.assertEqual((code, result["by"], result["counts"]["restored"]), (0, "cli", 1))
        self.assertEqual(self.store.get_message(first)["state"], "human_pending")
        self.assertEqual(self.run_cli("--ledger", path, "undo", str(info["action_id"]))[0], 1)
        self.assertEqual(self.run_cli("--ledger", path, "undo", "99")[0], 3)

    def test_conflict_lines_expiry_and_bad_input(self):
        path = str(self.config.ledger.path)
        mid = self.pending(1)
        info = self.post("/api/queue/actions", {"action": "assign", "ids": [mid], "lane": "support"})["undo"]
        self.store.set_state(mid, "done")
        code, out, _ = self.run_cli("--ledger", path, "undo", str(info["action_id"]))
        self.assertEqual(code, 0)
        self.assertIn("  " + mid + " conflict (now done in support)", out.splitlines())
        other = self.pending(2)
        late = self.post("/api/queue/actions", {"action": "done", "ids": [other]})["undo"]
        self.assertEqual(self.run_cli("--ledger", path, "undo", str(late["action_id"]),
                                      clock=lambda: NOW + 31)[:3:2], (1, "Undo window expired\n"))
        self.assertEqual(self.run_cli("--ledger", path, "undo")[0], 2)
        self.assertEqual(self.run_cli("--ledger", path, "undo", "--last", "1")[0], 2)
        self.assertEqual(self.run_cli("undo", "--last", "--ledger", str(self.root / "missing.db"))[0], 2)

    def test_config_supplies_the_ledger(self):
        mid = self.pending(1)
        self.post("/api/queue/actions", {"action": "done", "ids": [mid]})
        config = self.root / "dummy-config.yaml"
        data = sample_data()
        data["ledger"]["path"] = str(self.config.ledger.path)
        config.write_text(json.dumps(data), encoding="utf-8")
        code, out, _ = self.run_cli("--config", str(config), "undo", "--last")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.store.get_message(mid)["state"], "human_pending")
        bad = self.root / "bad-config.yaml"
        bad.write_text("ledger: [", encoding="utf-8")
        self.assertEqual(self.run_cli("--config", str(bad), "undo", "--last")[0], 2)


if __name__ == "__main__":
    unittest.main()
