"""P12 items 3 and 6: the card decision consumer with fixture decision files."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from slack_dispatch.config.schema import Decisions
from slack_dispatch.decisions import DecisionConsumer, canonical, card_dir, read_key, sign
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.dispatch_card import safe_name
from tests.test_pipeline_integration import event, fixture_config

OWNER = "U0000000093"
KEY_HEX = "ab" * 32  # dummy signing key for tests, built without a hex literal
KEY = bytes.fromhex(KEY_HEX)


def decision(message_id, lane, *, decided_by=OWNER, key=KEY):
    row = {"schema": "dispatch_decision/1", "message_id": message_id, "lane": lane,
           "decided_by": decided_by, "decided_at": "2026-01-01T09:00:00+09:00",
           "card_ts": "1767225600.000100"}
    row["hmac"] = sign(key, row)
    return row


class DecisionCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-decisions-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cards = self.root / "cards_dir"
        self.key_file = self.root / "keys" / "dispatch_hmac.env"
        self.key_file.parent.mkdir()
        self.key_file.write_text(f"DISPATCH_HMAC_KEY={KEY_HEX}\n", encoding="utf-8")
        config = fixture_config(self.root)
        self.config = replace(config, decisions=Decisions(dir=str(self.cards),
                                                          hmac_key_file=str(self.key_file)))
        self.store = LedgerStore(self.config.ledger.path)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id)
        self.ids = []
        for number in (1, 2, 3):
            mid = self.store.record_event(event(number)).message_id
            self.store.set_state(mid, "human_pending", note="none")
            self.ids.append(mid)
        self.consumer = DecisionConsumer(self.config, self.store, str(self.cards))

    def drop(self, row, name=None):
        folder = self.cards / "decisions"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (name or safe_name(row["message_id"]) + ".json")
        path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
        return path

    def files(self, folder):
        path = self.cards / "decisions" / folder
        return sorted(item.name for item in path.glob("*.json")) if path.exists() else []

    def inbox(self, lane):
        path = self.root / "inbox" / (lane + ".jsonl")
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def wakes(self):
        return [(row["lane"], row["message_id"], row["source"]) for row in self.store.pending_wake_requests()]


class DecisionTests(DecisionCase):
    def test_valid_lane_decision_assigns_like_the_console(self):
        path = self.drop(decision(self.ids[0], "support"))
        self.assertEqual(self.consumer.run_once(), 1)
        row = self.store.get_message(self.ids[0])
        self.assertEqual((row["state"], row["lane"], row["route_reason"]), ("assigned", "support", "human:card"))
        self.assertEqual([(entry["id"], entry["kind"]) for entry in self.inbox("support")], [(self.ids[0], "new")])
        self.assertEqual(self.wakes(), [("support", self.ids[0], "card")])
        self.assertFalse(path.exists())
        self.assertEqual(self.files("done"), [path.name])
        self.assertEqual(self.consumer.snapshot(), {"card_decisions": 1, "card_decisions_rejected": 0})

    def test_direct_assigns_the_console_direct_lane(self):
        self.drop(decision(self.ids[1], "direct"))
        self.assertEqual(self.consumer.run_once(), 1)
        self.assertEqual(self.store.get_message(self.ids[1])["lane"], self.config.console.direct_lane)
        self.assertEqual(self.wakes(), [("owner", self.ids[1], "card")])

    def test_direct_without_a_configured_direct_lane_is_rejected(self):
        self.consumer.config = replace(self.config, console=replace(self.config.console,
                                                                    direct_lane="owner-direct"))
        self.drop(decision(self.ids[1], "direct"))
        with self.assertLogs("slack_dispatch.decisions", level="WARNING") as logs:
            self.assertEqual(self.consumer.run_once(), 0)
        self.assertIn("reason=direct_lane", logs.output[0])
        self.assertEqual(self.store.get_message(self.ids[1])["state"], "human_pending")

    def test_exclude_marks_excluded_without_inbox_or_wake(self):
        self.drop(decision(self.ids[2], "exclude"))
        self.assertEqual(self.consumer.run_once(), 1)
        row = self.store.get_message(self.ids[2])
        self.assertEqual((row["state"], row["note"], row["lane"]), ("excluded", "human:card", None))
        self.assertEqual(self.wakes(), [])
        self.assertFalse((self.root / "inbox").exists())

    def test_bad_hmac_is_rejected_with_one_warning_and_no_change(self):
        bad = decision(self.ids[0], "support", key=bytes(32))
        path = self.drop(bad)
        with self.assertLogs("slack_dispatch.decisions", level="WARNING") as logs:
            self.assertEqual(self.consumer.run_once(), 0)
        self.assertEqual(logs.output, [f"WARNING:slack_dispatch.decisions:Card decision rejected "
                                       f"file={path.name} reason=hmac"])
        self.assertNotIn(KEY_HEX, "\n".join(logs.output))
        self.assertEqual(self.files("rejected"), [path.name])
        self.assertEqual(self.store.get_message(self.ids[0])["state"], "human_pending")
        self.assertEqual(self.consumer.snapshot(), {"card_decisions": 0, "card_decisions_rejected": 1})
        tampered = decision(self.ids[1], "support")
        tampered["lane"] = "content"
        self.drop(tampered)
        with self.assertLogs("slack_dispatch.decisions", level="WARNING"):
            self.consumer.run_once()
        self.assertEqual(self.store.get_message(self.ids[1])["state"], "human_pending")

    def test_unknown_message_and_other_user_are_rejected(self):
        missing = "slack:C0000000002:1999.000001"
        self.drop(decision(missing, "support"))
        self.drop(decision(self.ids[0], "support", decided_by="U0000000001"))
        with self.assertLogs("slack_dispatch.decisions", level="WARNING") as logs:
            self.assertEqual(self.consumer.run_once(), 0)
        reasons = sorted(line.rsplit("reason=", 1)[1] for line in logs.output)
        self.assertEqual(reasons, ["decided_by", "unknown_message"])
        self.assertEqual(len(self.files("rejected")), 2)

    def test_duplicate_decision_is_rejected_as_already_decided(self):
        first = self.drop(decision(self.ids[0], "support"))
        self.assertEqual(self.consumer.run_once(), 1)
        second = self.drop(decision(self.ids[0], "content"))
        with self.assertLogs("slack_dispatch.decisions", level="WARNING") as logs:
            self.assertEqual(self.consumer.run_once(), 0)
        self.assertIn("reason=already_decided", logs.output[0])
        self.assertEqual(self.store.get_message(self.ids[0])["lane"], "support")
        self.assertEqual(self.files("done"), [first.name])
        self.assertEqual(self.files("rejected"), [second.name])
        self.assertEqual(self.wakes(), [("support", self.ids[0], "card")])

    def test_missing_key_logs_once_and_leaves_files_untouched(self):
        moved = self.key_file.with_name("moved_aside.env")
        self.key_file.replace(moved)
        path = self.drop(decision(self.ids[0], "support"))
        with self.assertLogs("slack_dispatch.decisions", level="WARNING") as logs:
            self.assertEqual(self.consumer.run_once(), 0)
            self.assertEqual(self.consumer.run_once(), 0)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn(str(moved.parent), logs.output[0])
        self.assertTrue(path.exists())
        self.assertEqual(self.files("rejected"), [])
        moved.replace(self.key_file)
        self.assertEqual(self.consumer.run_once(), 1)

    def test_tmp_folder_is_ignored_and_malformed_files_are_rejected(self):
        tmp = self.cards / "decisions" / ".tmp"
        tmp.mkdir(parents=True)
        (tmp / (safe_name(self.ids[0]) + ".json")).write_text("{partial", encoding="utf-8")
        self.drop({"schema": "dispatch_decision/1"}, name=safe_name(self.ids[1]) + ".json")
        (self.cards / "decisions" / (safe_name(self.ids[2]) + ".json")).write_text("{broken", encoding="utf-8")
        self.drop(decision(self.ids[0], "support"), name="other_name.json")
        with self.assertLogs("slack_dispatch.decisions", level="WARNING") as logs:
            self.assertEqual(self.consumer.run_once(), 0)
        reasons = sorted(line.rsplit("reason=", 1)[1] for line in logs.output)
        self.assertEqual(reasons, ["file_name", "json", "keys"])
        self.assertEqual(len(list(tmp.iterdir())), 1)
        self.assertEqual(self.store.get_message(self.ids[0])["state"], "human_pending")

    def test_closed_items_and_context_rows_are_not_reopened(self):
        self.store.set_state(self.ids[0], "done", note="handled")
        self.store.set_route(self.ids[1], None, "context:root", state="done")
        self.drop(decision(self.ids[0], "support"))
        self.drop(decision(self.ids[1], "support"))
        with self.assertLogs("slack_dispatch.decisions", level="WARNING") as logs:
            self.assertEqual(self.consumer.run_once(), 0)
        reasons = sorted(line.rsplit("reason=", 1)[1] for line in logs.output)
        self.assertEqual(reasons, ["closed", "not_inbox_item"])

    def test_unknown_lane_is_rejected(self):
        self.drop(decision(self.ids[0], "unknown-lane"))
        with self.assertLogs("slack_dispatch.decisions", level="WARNING") as logs:
            self.assertEqual(self.consumer.run_once(), 0)
        self.assertIn("reason=lane", logs.output[0])


class HelperTests(unittest.TestCase):
    def test_canonical_form_matches_the_p11_contract(self):
        row = {"b": "2", "a": "café", "hmac": "ignored"}
        self.assertEqual(canonical(row), '{"a":"café","b":"2"}'.encode("utf-8"))
        self.assertEqual(canonical(row)[6:11], b"caf\xc3\xa9")

    def test_key_file_parsing_and_card_dir(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-key-") as temp:
            path = Path(temp) / "dispatch.env"
            path.write_text("# comment\nDISPATCH_HMAC_KEY=" + KEY_HEX + "\n", encoding="utf-8")
            self.assertEqual(read_key(path), KEY)
            path.write_text("DISPATCH_HMAC_KEY=short\n", encoding="utf-8")
            self.assertIsNone(read_key(path))
            self.assertIsNone(read_key(Path(temp) / "absent.env"))
            self.assertIsNone(read_key(None))
            config = fixture_config(Path(temp))
            self.assertIsNone(card_dir(config))
            self.assertEqual(card_dir(replace(config, decisions=Decisions(dir="dummy/cards"))), "dummy/cards")


if __name__ == "__main__":
    unittest.main()
