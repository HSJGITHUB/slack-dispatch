"""P16 `repair bot-flag`: every branch with a fake store and fake delivery, plus one
end-to-end apply against the real ledger and the default delivery."""
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from slack_dispatch.config.loader import load_config
from slack_dispatch.inbox import cli
from slack_dispatch.inbox.cli import main, repair_bot_flag, repair_command
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline
from tests.fakes.fake_store import FakeStore
from tests.test_pipeline_integration import SAMPLE

NOW = datetime(2026, 1, 31, tzinfo=timezone.utc)
RECENT = "2026-01-30T00:00:00+00:00"
OLD = "2025-12-01T00:00:00+00:00"
HUMAN, OTHER, UNKNOWN, BOT, KNOWN = ("U0000000071", "U0000000072", "U0000000073",
                                     "U0000000081", "U0000000082")
OWNER = "U0000000093"


class RepairStore(FakeStore):
    """FakeStore plus the P16 store methods the repair command uses."""

    def __init__(self, rows, directory):
        super().__init__(messages=rows)
        self.directory = directory
        for lane in ("owner", "support", "content"):
            self.upsert_lane(lane)

    def bot_flagged_messages(self, *, since=None):
        rows = [row for row in self.messages.values()
                if row.get("is_bot") == 1 and row.get("author")
                and (since is None or row["received_at"] >= since)]
        return [self.get_message(row["id"], include_body=False)
                for row in sorted(rows, key=lambda row: (row["received_at"], row["id"]))]

    def get_user_directory(self, user):
        flag = self.directory.get(user)
        return None if flag is None else {"user": user, "is_bot": flag}

    def set_is_bot(self, message_id, value):
        self.calls.append(("set_is_bot", message_id, int(bool(value))))
        self.messages[message_id]["is_bot"] = int(bool(value))

    def set_attention(self, message_id, attention):
        self.calls.append(("set_attention", message_id, attention))
        self.messages[message_id]["attention"] = attention


class FakeDelivery:
    def __init__(self, fail=False):
        self.fail = fail
        self.inboxes, self.notices, self.exports = [], [], 0

    def inbox(self, lane, message):
        if self.fail:
            raise OSError("dummy inbox failure")
        self.inboxes.append((lane, message["id"]))

    def human(self, message_id, kind, immediate):
        self.notices.append((message_id, kind, immediate))

    def export(self):
        self.exports += 1
        return True


def row(number, *, author=HUMAN, state="excluded", note="excluded:bot", lane=None,
        channel="C0000000002", body="plain", received_at=RECENT, attention=None, is_bot=1):
    return dict(id=f"slack:{channel}:{number}.0", source="slack", channel=channel,
                channel_type="channel", ts=f"{number}.0", thread_ts=None, author=author,
                state=state, note=note, lane=lane, body=body, received_at=received_at,
                attention=attention, is_bot=is_bot, route_reason=None)


def config():
    cfg = load_config(SAMPLE)
    return replace(cfg, bot_messages=replace(cfg.bot_messages, known_bot_users=(KNOWN,)))


class RepairBranchTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            row(1),                                                         # reroute to human
            row(2, channel="C0000000001"),                                  # reroute to support
            row(3, body="<@U0000000093> look"),                             # human with attention
            row(4, channel="C0000000001", body="<@U0000000092> fyi"),       # lane plus attention
            row(5, state="assigned", lane="support", note=None, body="<@U0000000093> hi"),
            row(6, state="acked", lane="support", note=None, body="plain"),
            row(7, state="done", lane="support", note=None),
            row(8, state="human_pending", note="none"),
            row(9, author=UNKNOWN),
            row(10, author=OWNER),
            row(11, author=BOT),                                            # directory bot
            row(12, author=KNOWN),                                          # known bot user
            row(13, author=None),                                           # no author
            row(14, received_at=OLD),                                       # outside window
            row(15, is_bot=0),                                              # not flagged
            row(16, state="assigned", lane="support", note=None, body="<@U0000000093> hi",
                attention="owner_mention"),                                 # attention already set
            dict(row(17, state="done", note="context"), route_reason="context:root"),  # context row
            row(18, state="done", note="context"),                          # context by note
            dict(row(19, state="done", note="owner_post"), route_reason="owner_post"),  # owner post
        ]
        self.store = RepairStore(self.rows, {HUMAN: 0, OTHER: 0, BOT: 1})
        self.delivery = FakeDelivery()
        self.config = config()

    def run_repair(self, **options):
        return {item["id"].split(":")[-1]: item for item in repair_bot_flag(
            self.store, self.config, delivery=self.delivery, now=NOW, **options)}

    def test_dry_run_plans_without_changes(self):
        before = json.dumps(self.store.messages, sort_keys=True)
        rows = self.run_repair()
        self.assertEqual({key: item["action"] for key, item in rows.items()}, {
            "1.0": "reroute", "2.0": "reroute", "3.0": "reroute", "4.0": "reroute",
            "5.0": "attention", "6.0": "attention", "7.0": "fix", "8.0": "fix",
            "9.0": "skip:unknown", "10.0": "refused:owner", "16.0": "attention",
            "17.0": "refused:context", "18.0": "refused:context", "19.0": "refused:owner_post"})
        self.assertTrue(all("result" not in item for item in rows.values()))
        self.assertEqual(json.dumps(self.store.messages, sort_keys=True), before)
        self.assertEqual((self.delivery.inboxes, self.delivery.notices), ([], []))

    def test_apply_every_branch(self):
        rows = self.run_repair(apply=True)
        results = {key: item["result"] for key, item in rows.items()}
        self.assertEqual(results, {
            "1.0": "rerouted:human", "2.0": "rerouted:support", "3.0": "rerouted:human",
            "4.0": "rerouted:support", "5.0": "attention:owner_mention", "6.0": "fixed",
            "7.0": "fixed", "8.0": "fixed", "9.0": "skipped:unknown", "10.0": "refused:owner",
            "16.0": "fixed", "17.0": "refused:context", "18.0": "refused:context",
            "19.0": "refused:owner_post"})
        messages = self.store.messages
        for number in (1, 2, 3, 4, 5, 6, 7, 8, 16):
            self.assertEqual(messages[f"slack:{self.rows[number - 1]['channel']}:{number}.0"]["is_bot"], 0)
        for number in (9, 10, 11, 12, 17, 18, 19):
            self.assertEqual(messages[f"slack:C0000000002:{number}.0"]["is_bot"], 1)
        self.assertEqual(messages["slack:C0000000002:17.0"]["state"], "done")
        self.assertEqual(messages["slack:C0000000002:1.0"]["state"], "human_pending")
        self.assertEqual(messages["slack:C0000000001:2.0"]["state"], "assigned")
        self.assertEqual(messages["slack:C0000000001:2.0"]["lane"], "support")
        self.assertEqual(messages["slack:C0000000002:3.0"]["attention"], "owner_mention")
        self.assertEqual(messages["slack:C0000000001:4.0"]["attention"], "owner_mention")
        self.assertEqual(messages["slack:C0000000002:5.0"]["attention"], "owner_mention")
        self.assertEqual(messages["slack:C0000000002:7.0"]["state"], "done")
        self.assertEqual(self.delivery.inboxes, [("support", "slack:C0000000001:2.0"),
                                                 ("support", "slack:C0000000001:4.0")])
        self.assertEqual(sorted(self.delivery.notices, key=lambda notice: notice[0][-3:]), [
            ("slack:C0000000002:1.0", "message", False),
            ("slack:C0000000002:3.0", "attention:owner_mention", True),
            ("slack:C0000000001:4.0", "attention:owner_mention", True),
            ("slack:C0000000002:5.0", "attention:owner_mention", True)])
        self.assertEqual(self.delivery.exports, 2)
        # The new root thread is bound to the lane for later replies.
        self.assertEqual(self.store.lookup_binding("slack", "C0000000001", "2.0"), "support")

    def test_assume_human_includes_unknown_authors(self):
        rows = self.run_repair(apply=True, assume_human=True)
        self.assertEqual(rows["9.0"]["result"], "rerouted:human")
        self.assertEqual(self.store.messages["slack:C0000000002:9.0"]["is_bot"], 0)

    def test_since_days_widens_the_window(self):
        self.assertIn("14.0", self.run_repair(since_days=90))
        self.assertNotIn("14.0", self.run_repair(since_days=30))

    def test_error_is_reported_per_row(self):
        self.delivery = FakeDelivery(fail=True)
        rows = self.run_repair(apply=True)
        self.assertEqual(rows["2.0"]["result"], "error")
        self.assertEqual(rows["2.0"]["error_class"], "OSError")
        # Delivery runs before any mutation, so the failed row is unchanged and retried later.
        failed = self.store.messages["slack:C0000000001:2.0"]
        self.assertEqual((failed["is_bot"], failed["state"], failed["note"]), (1, "excluded", "excluded:bot"))
        self.assertEqual(rows["1.0"]["result"], "rerouted:human")

    def args(self, *extra):
        return cli.build_parser().parse_args(["repair", "bot-flag", "--config", str(SAMPLE), *extra])

    def test_cli_text_and_json_and_exit_codes(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(cli, "optional_config", return_value=self.config):
            code = repair_command(self.args(), "unused", self.store, out, err,
                                  delivery=self.delivery, now=NOW)
        self.assertEqual(code, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "repair bot-flag mode=dry-run since_days=30 rows=14")
        self.assertIn("slack:C0000000002:18.0 U0000000071 done - context refused:context", lines)
        self.assertIn("slack:C0000000002:1.0 U0000000071 excluded - excluded:bot reroute", lines)
        self.assertIn("slack:C0000000002:10.0 U0000000093 excluded - excluded:bot refused:owner", lines)
        self.assertEqual(lines[-1], "dry run: nothing changed; rerun with --apply")
        out = io.StringIO()
        with patch.object(cli, "optional_config", return_value=self.config):
            code = repair_command(self.args("--apply", "--json"), "unused", self.store, out, err,
                                  delivery=FakeDelivery(fail=True), now=NOW)
        self.assertEqual(code, 1)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["mode"], "apply")
        self.assertEqual(payload["counts"]["error"], 2)
        self.assertEqual(payload["counts"]["refused:owner"], 1)

    def test_cli_requires_config_and_positive_days(self):
        err = io.StringIO()
        with patch.object(cli, "optional_config", return_value=None):
            self.assertEqual(repair_command(self.args(), "unused", self.store, io.StringIO(), err), 2)
        self.assertIn("Config is required", err.getvalue())
        err = io.StringIO()
        self.assertEqual(main(["repair", "bot-flag", "--since-days", "0"], store=self.store,
                              stdout=io.StringIO(), stderr=err), 2)
        self.assertEqual(main(["repair", "bot-flag", "--since-days", "x"], store=self.store,
                              stdout=io.StringIO(), stderr=io.StringIO()), 2)


class RepairLedgerTests(unittest.TestCase):
    """The real ledger and the default delivery: inbox backfill record and queued notice."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p16-repair-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = LedgerStore(self.root / "ledger.sqlite3", store_body=True)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in ("owner", "support", "content"):
            self.store.upsert_lane(lane)
        received = datetime.now().astimezone().isoformat()
        for number, channel, text in ((1, "C0000000001", "plain"),
                                      (2, "C0000000002", "<@U0000000093> look")):
            self.store.record_event(dict(source="slack", event_id=f"EvDUMMY{number}", event_type="message",
                                         channel=channel, channel_type="channel", user=HUMAN,
                                         ts=f"{number}.0", received_at=received, text=text,
                                         bot_id="B0000000071", is_bot=True))
            self.store.set_state(f"slack:{channel}:{number}.0", "excluded", note="excluded:bot")
        self.store.put_user_directory(dict(user=HUMAN, is_bot=0, is_app_user=0, deleted=0, name="dummy"),
                                      received)
        cfg = load_config(SAMPLE)
        self.config_path = self.root / "config.yaml"
        text = SAMPLE.read_text(encoding="utf-8").replace("state_dir: state", f"state_dir: {self.root.as_posix()}/state")
        # The sample has no human_queue adapter; make the file drop one so notices queue.
        text = text.replace("  - kind: file_drop\n", "  - kind: file_drop\n    purpose: human_queue\n")
        self.config_path.write_text(text, encoding="utf-8")
        self.assertEqual(load_config(self.config_path).lanes, cfg.lanes)

    def test_apply_end_to_end(self):
        out = io.StringIO()
        code = main(["repair", "bot-flag", "--apply", "--config", str(self.config_path),
                     "--inbox-root", str(self.root / "inbox")], store=self.store, stdout=out,
                    stderr=io.StringIO())
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("rerouted:support", out.getvalue())
        self.assertIn("rerouted:human", out.getvalue())
        record, = [json.loads(line) for line in
                   (self.root / "inbox/support.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual((record["id"], record["kind"]), ("slack:C0000000001:1.0", "backfill"))
        notices = self.store._rows("SELECT message_id, kind, immediate, sent_at FROM human_notices")
        self.assertEqual({(row["message_id"], row["kind"], row["immediate"]) for row in notices},
                         {("slack:C0000000002:2.0", "attention:owner_mention", 1)})
        self.assertTrue(all(row["sent_at"] is None for row in notices))
        # The running service's flush delivers the queued immediate notice.
        receivers = [Mock(), Mock(), Mock()]
        cfg = load_config(self.config_path)
        pipeline = Pipeline(cfg, self.store, None, receivers,
                            WakePolicyEngine(cfg.wake_policy, lambda: 5000.0), lambda: 5000.0)
        pipeline.flush(5000.0)
        receivers[2].notify.assert_called_once_with("human_queue", ["slack:C0000000002:2.0"])
        self.assertEqual(self.store.get_message("slack:C0000000002:2.0")["state"], "human_pending")
        self.assertEqual(self.store.get_message("slack:C0000000002:2.0")["is_bot"], 0)
        self.assertTrue((self.root / "state/human_queue.jsonl").exists())
        self.assertEqual(self.store.bot_flagged_messages(), [])

    def test_store_queries(self):
        flagged = self.store.bot_flagged_messages(since="2000-01-01T00:00:00+00:00")
        self.assertEqual([row["id"] for row in flagged], ["slack:C0000000001:1.0", "slack:C0000000002:2.0"])
        self.assertTrue(all("body" not in row for row in flagged))
        self.assertEqual(self.store.bot_flagged_messages(since="2999-01-01T00:00:00+00:00"), [])
        self.assertEqual([row["user"] for row in self.store.list_user_directory()], [HUMAN])
        self.assertIsNone(self.store.get_user_directory(UNKNOWN))
        self.store.set_is_bot("slack:C0000000001:1.0", False)
        self.assertEqual(len(self.store.bot_flagged_messages()), 1)


if __name__ == "__main__":
    unittest.main()
