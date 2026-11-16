"""P19 fix1: journal safety, inbox rows without a lane, binding restore, nothing_restored and
the inbox CLI `undo` command (dummy ids only)."""
import io
import json
import sqlite3
import unittest
from unittest.mock import patch

from slack_dispatch.inbox import cli
from tests.test_console_app import NOW, seed, stamp, ts
from tests.test_console_undo import UndoCase


class JournalSafetyTests(UndoCase):
    def broken(self, name):
        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("dummy failure")
        return patch.object(self.store, name, fail)

    def test_journal_write_failure_keeps_the_action(self):
        mid = self.pending(1)
        with self.broken("add_console_action"), \
                self.assertLogs("slack_dispatch.console.actions", level="WARNING") as logs:
            data = self.post("/api/queue/actions", {"action": "done", "ids": [mid]})
        self.assertEqual((data["applied"], data["undo"], data["journal"]), (1, None, "failed"))
        self.assertEqual(logs.output, ["WARNING:slack_dispatch.console.actions:Console journal "
                                       "failed action=done class=OperationalError"])
        self.assertEqual(self.store.get_message(mid)["state"], "done")
        self.assertEqual(self.journal(), [])

    def test_journal_start_failure_keeps_the_commit(self):
        first, second = self.pending(1), self.pending(2)
        with self.broken("wake_request_mark"), self.assertLogs("slack_dispatch.console.actions",
                                                               level="WARNING"):
            data = self.post("/api/queue/commit", {"decisions": [
                {"id": first, "action": "assign", "lane": "support"},
                {"id": second, "action": "exclude_channel"}]})
        self.assertEqual(data["counts"]["applied"], 2)
        self.assertEqual((data["undo"], data["journal"]), (None, "failed"))
        self.assertEqual(self.store.get_message(first)["state"], "excluded")  # same channel
        self.assertEqual(self.store.channel_exclusion_ids(), {"C0000000002"})

    def test_snapshot_failure_keeps_the_lane_action(self):
        mid = self.lane_item(1)
        with self.broken("message_snapshots"), self.assertLogs("slack_dispatch.console.actions",
                                                               level="WARNING"):
            data = self.post("/api/lanes/support/actions", {"action": "reroute", "ids": [mid],
                                                            "lane": "content"})
        self.assertEqual((data["applied"], data["undo"], data["journal"]), (1, None, "failed"))
        self.assertEqual(self.store.get_message(mid)["lane"], "content")

    def test_ok_status(self):
        mid = self.pending(1)
        data = self.post("/api/queue/actions", {"action": "done", "ids": [mid]})
        self.assertEqual(data["journal"], "ok")
        commit = self.post("/api/queue/commit", {"decisions": [{"id": mid, "action": "done"}]})
        self.assertEqual((commit["undo"], commit["journal"]), (None, "ok"))


class InboxWithoutLaneTests(UndoCase):
    def inbox_ids(self):
        out = io.StringIO()
        code = cli.main(["--inbox-root", str(self.inbox), "inbox", "support", "--json"],
                        store=self.store, stdout=out, stderr=io.StringIO())
        self.assertEqual(code, 0)
        return [row["id"] for row in json.loads(out.getvalue() or "[]")]

    def console_ids(self):
        groups = self.get("/api/lanes/support/inbox")["groups"]
        return [group["root"]["id"] for group in groups] + [
            item["id"] for group in groups for item in group["items"]]

    def test_undone_assign_leaves_the_lane_inbox(self):
        mid = self.pending(1)
        kept = self.pending(2)
        info = self.post("/api/queue/commit", {"decisions": [
            {"id": mid, "action": "assign", "lane": "support"},
            {"id": kept, "action": "assign", "lane": "support"}]})["undo"]
        self.assertEqual(sorted(self.inbox_ids()), sorted([mid, kept]))
        self.store.set_state(kept, "acked")  # the lane took this one meanwhile
        self.undo(info["action_id"])
        self.assertEqual(self.store.get_message(mid)["state"], "human_pending")
        self.assertEqual(self.inbox_ids(), [kept])
        self.assertNotIn(mid, self.console_ids())

    def test_excluded_row_without_lane_is_hidden(self):
        mid = self.pending(1)
        self.post("/api/queue/actions", {"action": "assign", "ids": [mid], "lane": "support"})
        self.store.set_route(mid, None, "test", state="excluded")
        self.assertEqual(self.inbox_ids(), [])


class BindingRestoreTests(UndoCase):
    def binding(self, mid):
        message = self.store.get_message(mid)
        return self.store.lookup_binding("slack", message["channel"],
                                         message["thread_ts"] or message["ts"])

    def test_assign_undo_removes_the_new_binding(self):
        mid = self.pending(1)
        info = self.post("/api/queue/actions", {"action": "assign", "ids": [mid], "lane": "support"})["undo"]
        self.assertEqual(self.binding(mid), "support")
        before = json.loads(self.store.get_console_action(info["action_id"])["before"])[0]
        self.assertEqual(before["binding"], {"source": "slack", "channel": "C0000000002",
                                             "thread_ts": ts(1), "lane": None})
        result = self.undo(info["action_id"])
        self.assertEqual((result["results"][0]["binding"], result["counts"]["binding_restored"]),
                         ("restored", 1))
        self.assertIsNone(self.binding(mid))

    def test_reroute_undo_restores_the_earlier_binding(self):
        mid = self.lane_item(1)
        self.store.bind_thread("slack", "C0000000002", ts(1), "support")
        info = self.post("/api/lanes/support/actions", {"action": "reroute", "ids": [mid],
                                                        "lane": "content"})["undo"]
        self.assertEqual(self.binding(mid), "content")
        self.undo(info["action_id"])
        self.assertEqual(self.binding(mid), "support")

    def test_binding_changed_meanwhile_is_reported_and_kept(self):
        mid = self.pending(1)
        info = self.post("/api/queue/actions", {"action": "assign", "ids": [mid], "lane": "support"})["undo"]
        self.store.bind_thread("slack", "C0000000002", ts(1), "owner")  # rebound meanwhile
        result = self.undo(info["action_id"])
        self.assertEqual((result["results"][0]["status"], result["results"][0]["binding"]),
                         ("restored", "binding_conflict"))
        self.assertEqual(result["counts"]["binding_conflict"], 1)
        self.assertEqual(self.binding(mid), "owner")
        self.assertEqual(self.store.get_message(mid)["state"], "human_pending")

    def test_two_items_of_one_thread_restore_the_binding_once(self):
        root = self.pending(1)
        reply = self.pending(2, thread=1)
        info = self.post("/api/queue/commit", {"decisions": [
            {"id": root, "action": "assign", "lane": "support"},
            {"id": reply, "action": "assign", "lane": "content"}]})["undo"]
        # P24: assigning the reply keeps the binding the root assignment made.
        self.assertEqual(self.binding(root), "support")
        result = self.undo(info["action_id"])
        # P24: only the root assignment changed the binding, so only it carries one.
        self.assertEqual([row["binding"] for row in result["results"]], ["restored", None])
        self.assertIsNone(self.binding(root))

    def test_actions_without_binding_change_carry_none(self):
        mid = self.pending(1)
        info = self.post("/api/queue/actions", {"action": "done", "ids": [mid]})["undo"]
        self.assertNotIn("binding", json.loads(self.store.get_console_action(info["action_id"])["before"])[0])
        self.assertIsNone(self.undo(info["action_id"])["results"][0]["binding"])


class NothingRestoredTests(UndoCase):
    def test_channel_exclusion_kept_when_every_item_conflicts(self):
        clicked = self.pending(1)
        other = self.pending(2)
        info = self.post("/api/queue/actions", {"action": "exclude_channel", "ids": [clicked]})["undo"]
        for mid in (clicked, other):
            self.store.set_route(mid, "support", "test")  # handled meanwhile
        result = self.undo(info["action_id"])
        self.assertTrue(result["nothing_restored"])
        self.assertEqual((result["counts"]["conflict"], result["counts"]["channel_exclusions_ended"]),
                         (2, 0))
        self.assertEqual(self.store.channel_exclusion_ids(), {"C0000000002"})
        self.assertIsNotNone(self.store.get_console_action(info["action_id"])["undone_at"])

    def test_partial_conflict_still_ends_the_exclusion(self):
        clicked = self.pending(1)
        other = self.pending(2)
        info = self.post("/api/queue/actions", {"action": "exclude_channel", "ids": [clicked]})["undo"]
        self.store.set_route(other, "support", "test")
        result = self.undo(info["action_id"])
        self.assertFalse(result["nothing_restored"])
        self.assertEqual(result["counts"]["channel_exclusions_ended"], 1)
        self.assertEqual(self.store.channel_exclusion_ids(), set())


class InboxCliUndoTests(UndoCase):
    def run_cli(self, *argv, now=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(cli, "UNDO_CLOCK", lambda: NOW if now is None else now):
            code = cli.main(list(argv), store=self.store, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_undo_command(self):
        self.assertEqual(self.run_cli("undo", "--last")[0], 3)
        first = self.pending(1)
        second = seed(self.store, ts(2), received=stamp(2))
        self.store.set_state(second, "human_pending", note="none")
        older = self.post("/api/queue/actions", {"action": "done", "ids": [first]})["undo"]
        self.post("/api/queue/actions", {"action": "exclude", "ids": [second]})
        code, out, err = self.run_cli("undo", "--last")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("undone action 2 (exclude): restored 1"))
        self.assertEqual(self.store.get_message(second)["state"], "human_pending")
        code, out, _ = self.run_cli("undo", str(older["action_id"]), "--json")
        self.assertEqual((code, json.loads(out)["by"]), (0, "cli"))
        self.assertEqual(self.store.get_message(first)["state"], "human_pending")
        self.assertEqual(self.run_cli("undo", str(older["action_id"]))[0], 1)
        self.assertEqual(self.run_cli("undo", "77")[0], 3)
        self.assertEqual(self.run_cli("undo")[0], 2)
        self.assertEqual(self.run_cli("undo", "--last", "1")[0], 2)
        third = self.pending(3)
        late = self.post("/api/queue/actions", {"action": "done", "ids": [third]})["undo"]
        code, _, err = self.run_cli("undo", str(late["action_id"]), now=NOW + 31)
        self.assertEqual((code, err), (1, "Undo window expired\n"))


if __name__ == "__main__":
    unittest.main()
