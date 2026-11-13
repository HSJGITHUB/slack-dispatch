"""P12 item 4: global --config, message_deleted handling and context-row refusals."""
import io
import json

import yaml

from slack_dispatch.inbox.cli import main
from slack_dispatch.inbox.files import append, record
from tests.test_cutover_command import LedgerCase, iso
from tests.test_pipeline_integration import SAMPLE


class GlobalConfigTests(LedgerCase):
    def write_config(self):
        data = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        data["people"] = {"U0000000088": "Dummy Author"}
        path = self.root / "config.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return str(path)

    def test_config_before_and_after_the_subcommand(self):
        self.add(received=10)
        config = self.write_config()
        global_form = self.cli("--config", config, "inbox", "support", "--json")
        sub_form = self.cli("inbox", "support", "--json", "--config", config)
        self.assertEqual(global_form[0], 0, global_form[2])
        self.assertEqual(global_form, sub_form)
        self.assertEqual(json.loads(global_form[1])[0]["author"], "Dummy Author")
        code, out, err = self.cli("--config", config, "inbox", "support", "--thread", "--no-body")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("author Dummy Author", out)

    def test_global_config_is_accepted_by_every_command(self):
        mid = self.add(received=10)
        config = self.write_config()
        self.assertEqual(self.cli("--config", config, "ack", mid)[0], 0)
        self.assertEqual(self.cli("--config", config, "cutover", "support")[0], 0)
        self.assertEqual(self.cli("ack", mid, "--config", config)[0], 0)
        missing = str(self.root / "missing.yaml")
        self.assertEqual(self.cli("--config", missing, "inbox", "support")[0], 2)


class DeletedMessageTests(LedgerCase):
    def delete(self, mid, number=1):
        row = self.store.get_message(mid)
        self.store.record_event(dict(source="slack", event_id=f"EvDEL{number:04d}", event_type="message",
                                     subtype="message_deleted", channel=row["channel"], ts=row["ts"],
                                     received_at=iso(900)))
        return self.store.get_message(mid, include_body=True)

    def test_open_row_is_closed_marked_and_hidden(self):
        mid = self.add(received=10, body="DUMMY_SECRET_BODY")
        row = self.delete(mid)
        self.assertEqual((row["state"], row["deleted_at"], row["done_at"]), ("done", iso(900), iso(900)))
        self.assertEqual(row["note"], "message_deleted")
        self.assertIsNone(row["body"])
        self.assertNotIn(mid, self.cli("inbox", "support")[1])
        code, out, _ = self.cli("inbox", "support", "--include-done", "--json")
        listed = json.loads(out)
        self.assertEqual([(entry["id"], entry["state"]) for entry in listed], [(mid, "deleted")])
        self.assertNotIn("DUMMY_SECRET_BODY", self.cli("inbox", "support", "--include-done", "--show-body")[1])

    def test_closed_row_keeps_its_state_and_thread_view_hides_deleted_reply(self):
        root = self.add(received=10, body="DUMMY_ROOT")
        reply = self.add(received=20, thread=self.store.get_message(root)["ts"], body="DUMMY_REPLY")
        other = self.add(received=30, state="excluded", lane=None, deliver=False)
        self.delete(reply, 1)
        self.assertEqual(self.delete(other, 2)["state"], "excluded")
        code, out, _ = self.cli("inbox", "support", "--thread", "--json")
        groups = json.loads(out)
        self.assertEqual([group["root"]["id"] for group in groups], [root])
        self.assertEqual(groups[0]["items"], [])
        self.delete(root, 3)
        self.assertEqual(json.loads(self.cli("inbox", "support", "--thread", "--json")[1]), [])

    def test_deleted_root_with_pending_reply_shows_a_marker(self):
        root = self.add(received=10, body="DUMMY_ROOT", deliver=False)
        reply = self.add(received=20, thread=self.store.get_message(root)["ts"])
        self.delete(root)
        groups = json.loads(self.cli("inbox", "support", "--thread", "--json")[1])
        self.assertEqual(groups[0]["root"]["state"], "deleted")
        self.assertEqual(groups[0]["root"]["body"], "[deleted]")
        self.assertEqual([item["id"] for item in groups[0]["items"]], [reply])


class ContextRowTests(LedgerCase):
    def test_assign_and_reroute_refuse_context_rows_and_owner_posts(self):
        context = self.add(lane=None, state="done", reason="context:root", note="context", deliver=False)
        owner = self.add(lane=None, state="done", reason="owner_post", note="owner_post", deliver=False)
        for args, mid, kind in ((("assign", context, "support"), context, "a context row"),
                                (("reroute", owner, "--to", "content"), owner, "an owner post")):
            with self.subTest(args=args):
                code, out, err = self.cli(*args)
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(err, f"refused: {mid} is {kind}, not an inbox item\n")
                self.assertEqual(self.store.get_message(mid)["state"], "done")
        self.assertEqual(self.records("support"), [])
        self.assertEqual(self.records("content"), [])
        self.assertEqual(self.store._rows("SELECT COUNT(*) AS n FROM wake_requests")[0]["n"], 0)

    def test_thread_view_shows_context_rows_only_as_root_or_owner_marker(self):
        root = self.add(lane=None, state="done", reason="context:root", note="context", deliver=False)
        thread = self.store.get_message(root)["ts"]
        reply = self.add(received=20, thread=thread)
        owner = self.add(lane=None, state="done", reason="owner_post", note="owner_post", thread=thread,
                         deliver=False)
        # A record written before P12 (an earlier assign of the owner post) is not listed.
        append(self.inbox, "support", [record(dict(self.store.get_message(owner), id=owner), "new")])
        listed = [row["id"] for row in json.loads(self.cli("inbox", "support", "--include-done",
                                                           "--json")[1])]
        self.assertEqual(listed, [reply])
        groups = json.loads(self.cli("inbox", "support", "--thread", "--json")[1])
        self.assertEqual(groups[0]["root"]["id"], root)
        self.assertEqual([item["id"] for item in groups[0]["items"]], [reply])
        self.assertEqual(groups[0]["owner_replied_at"], self.store.get_message(owner)["ts"])

    def test_cli_assign_and_reroute_queue_wake_requests(self):
        mid = self.add(lane=None, state="human_pending", deliver=False)
        self.assertEqual(self.cli("assign", mid, "support")[0], 0)
        self.assertEqual(self.cli("reroute", mid, "--to", "content")[0], 0)
        rows = self.store.pending_wake_requests()
        self.assertEqual([(row["lane"], row["message_id"], row["source"]) for row in rows],
                         [("support", mid, "cli"), ("content", mid, "cli")])
        self.assertEqual(self.store.get_message(mid)["route_reason"], "reroute:support")

    def test_main_without_injected_store_reads_the_ledger_path(self):
        mid = self.add(received=10)
        self.store.close()
        out, err = io.StringIO(), io.StringIO()
        code = main(["--ledger", str(self.root / "ledger.sqlite3"), "--inbox-root", str(self.inbox),
                     "cutover", "support"], stdout=out, stderr=err)
        self.store.open()
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertIn(mid + " kept_open", out.getvalue())
