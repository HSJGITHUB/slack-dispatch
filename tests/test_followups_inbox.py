"""P9 follow-ups: edited means edited, UTF-8 console output, people names and `when`."""
from dataclasses import replace
from datetime import timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

import yaml

from slack_dispatch.config.loader import ConfigError, parse_config
from slack_dispatch.inbox.cli import main, when
from slack_dispatch.inbox.files import record
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline
from slack_dispatch.router.classifier import NullSuggester
from tests.test_pipeline_integration import SAMPLE, event, fixture_config

ROOT_TS = "1700000000.000100"  # 2023-11-14 22:13:20 UTC
REPLY_TS = "1700000060.000200"
QUOTE = "‛dummyé"  # characters a cp949 console cannot encode


class EditedTests(unittest.TestCase):
    """Item 5: kind followup only for message_changed; recency replies stay new/backfill."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p9-inbox-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = fixture_config(self.root)
        self.store = LedgerStore(self.config.ledger.path)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id)
        self.now = 1000.0
        clock = lambda: self.now  # noqa: E731
        notifiers = [FileInboxNotifier(self.config.inbox.root, self.store.get_message), None, None]
        self.config = replace(self.config, notify_adapters=self.config.notify_adapters[:1])
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(), notifiers[:1],
                                 WakePolicyEngine(self.config.wake_policy, clock), clock)

    def records(self):
        path = self.root / "inbox/support.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def listing(self):
        out = io.StringIO()
        code = main(["--inbox-root", str(self.root / "inbox"), "inbox", "support", "--json"],
                    store=self.store, stdout=out, stderr=io.StringIO())
        self.assertEqual(code, 0)
        return {row["id"]: row for row in json.loads(out.getvalue())}

    def test_recent_reply_is_not_edited_until_message_changed(self):
        root = self.pipeline.handle_event(event())
        self.now += 10
        reply = self.pipeline.handle_event(event(2, text="plain", thread_ts=event()["ts"],
                                                 is_thread_reply=True))
        self.assertTrue(self.store.get_message(reply.message_id)["followup"])
        self.assertEqual([(row["kind"], row["followup"]) for row in self.records()],
                         [("new", False), ("new", True)])
        rows = self.listing()
        self.assertEqual((rows[reply.message_id]["kind"], rows[reply.message_id]["edited"]), ("new", "no"))
        self.assertEqual(rows[root.message_id]["edited"], "no")
        self.pipeline.handle_event(event(3, ts=event(2)["ts"], subtype="message_changed",
                                         thread_ts=event()["ts"], is_thread_reply=True))
        self.assertEqual((self.records()[-1]["kind"], self.records()[-1]["followup"]), ("followup", True))
        rows = self.listing()
        self.assertEqual((rows[reply.message_id]["kind"], rows[reply.message_id]["edited"]), ("new", "yes"))

    def test_backfill_burst_replies_keep_backfill_kind(self):
        self.pipeline.handle_event(event(origin="reconcile"))
        for number in range(2, 5):
            self.pipeline.handle_event(event(number, origin="reconcile", thread_ts=event()["ts"],
                                             is_thread_reply=True))
        self.assertEqual([row["kind"] for row in self.records()], ["backfill"] * 4)
        self.assertTrue(all(row["edited"] == "no" for row in self.listing().values()))

    def test_record_kind_derivation(self):
        base = {"id": "slack:CDUMMY:1", "channel": "CDUMMY", "ts": "1"}
        self.assertEqual(record(dict(base, followup=True))["kind"], "new")
        self.assertEqual(record(dict(base, followup=True, origin="reconcile"))["kind"], "backfill")
        self.assertEqual(record(dict(base, followup=True, edited_at="2026-01-01T00:00:00+00:00"))["kind"],
                         "followup")
        self.assertEqual(record(dict(base, edited_at="x"), "reroute")["kind"], "reroute")

    def test_legacy_followup_flag_alone_is_not_edited(self):
        mid = self.store.record_event(event()).message_id
        self.store.set_route(mid, "support", "fixture")
        row = dict(record(self.store.get_message(mid)), followup=True)
        (self.root / "inbox").mkdir()
        (self.root / "inbox/support.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        self.assertEqual(self.listing()[mid]["edited"], "no")


class DisplayTests(unittest.TestCase):
    """Item 6: UTF-8 console streams, people names and the `when` column."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p9-display-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = LedgerStore(self.root / "dummy.db", store_body=True)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        self.store.upsert_lane("support")
        self.root_id = self.add(ROOT_TS, author="UDUMMY1", body="root " + QUOTE)
        self.reply_id = self.add(REPLY_TS, thread=ROOT_TS, author="UDUMMY2", body="reply " + QUOTE)
        self.raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        self.raw["people"] = {"UDUMMY1": "Dummy One"}
        self.raw.pop("display_timezone", None)

    def add(self, ts, thread=None, author="UDUMMY1", body="dummy"):
        mid = "slack:CDUMMY:" + ts
        self.store.record_event(dict(source="slack", event_type="message", event_id="EvDUMMY" + ts.replace(".", ""),
                                     channel="CDUMMY", ts=ts, thread_ts=thread, user=author, text=body,
                                     received_at="2026-01-01T00:00:00+00:00"))
        self.store.set_route(mid, "support", "test")
        row = record(self.store.get_message(mid))
        with (self.root / "support.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        return mid

    def config(self, **changes):
        path = self.root / "config.yaml"
        path.write_text(yaml.safe_dump(dict(self.raw, **changes)), encoding="utf-8")
        return str(path)

    def cli(self, *args, env=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {"SLACK_DISPATCH_CONFIG": "", **(env or {})}):
            code = main(["--inbox-root", str(self.root), *args], store=self.store, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_console_streams_are_reconfigured_to_utf8(self):
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp949", errors="strict")
        stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp949", errors="strict")
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr), \
             patch.dict("os.environ", {"SLACK_DISPATCH_CONFIG": ""}):
            code = main(["--inbox-root", str(self.root), "inbox", "support", "--thread"], store=self.store)
        self.assertEqual(code, 0)
        self.assertEqual((stdout.encoding, stdout.errors), ("utf-8", "replace"))
        self.assertEqual((stderr.encoding, stderr.errors), ("utf-8", "replace"))
        stdout.flush()
        self.assertIn("reply " + QUOTE, stdout.buffer.getvalue().decode("utf-8"))
        injected = io.TextIOWrapper(io.BytesIO(), encoding="cp949")
        main(["--inbox-root", str(self.root), "inbox", "support"], store=self.store, stdout=injected,
             stderr=io.StringIO())
        self.assertEqual(injected.encoding, "cp949")  # caller-owned streams are left alone

    def test_listing_and_json_without_config_use_ids_and_utc(self):
        code, out, err = self.cli("inbox", "support")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], "id age when channel thread kind edited attention author state"
                         " channel_name author_name")
        self.assertIn(" 11-14 22:13 CDUMMY ", lines[1])
        self.assertIn(" UDUMMY1 assigned", lines[1])
        code, out, _ = self.cli("inbox", "support", "--json")
        rows = {row["id"]: row for row in json.loads(out)}
        self.assertEqual(rows[self.reply_id]["ts"], REPLY_TS)
        self.assertEqual(rows[self.reply_id]["when"], "11-14 22:14")
        self.assertEqual((rows[self.root_id]["author"], rows[self.root_id]["author_id"]),
                         ("UDUMMY1", "UDUMMY1"))
        self.assertIn("age", rows[self.root_id])

    def test_people_names_in_listing_and_thread_view(self):
        config = self.config()
        for args, env in ((["--config", config], None), ([], {"SLACK_DISPATCH_CONFIG": config})):
            with self.subTest(args=args):
                code, out, err = self.cli("inbox", "support", *args, env=env)
                self.assertEqual((code, err), (0, ""))
                self.assertIn("Dummy One", out)
                self.assertIn("UDUMMY2", out)
        code, out, _ = self.cli("inbox", "support", "--json", "--config", config)
        rows = {row["id"]: row for row in json.loads(out)}
        self.assertEqual((rows[self.root_id]["author"], rows[self.root_id]["author_id"]),
                         ("Dummy One", "UDUMMY1"))
        self.assertEqual(rows[self.reply_id]["author"], "UDUMMY2")
        code, out, _ = self.cli("inbox", "support", "--thread", "--json", "--config", config)
        group, = json.loads(out)
        self.assertEqual((group["root"]["author"], group["root"]["author_id"]), ("Dummy One", "UDUMMY1"))
        self.assertEqual(group["root"]["when"], "11-14 22:13")
        item, = group["items"]
        self.assertEqual((item["author"], item["author_id"], item["ts"], item["when"]),
                         ("UDUMMY2", "UDUMMY2", REPLY_TS, "11-14 22:14"))
        code, out, _ = self.cli("inbox", "support", "--thread", "--config", config)
        self.assertIn("author Dummy One ts " + ROOT_TS + " when 11-14 22:13", out)

    def test_display_timezone_converts_and_falls_back_to_utc(self):
        config = self.config(display_timezone="Asia/Seoul")
        with patch("zoneinfo.ZoneInfo", return_value=timezone(timedelta(hours=9))):
            code, out, err = self.cli("inbox", "support", "--json", "--config", config)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual({row["when"] for row in json.loads(out)}, {"11-15 07:13", "11-15 07:14"})
        with patch("zoneinfo.ZoneInfo", side_effect=ZoneInfoNotFoundError("dummy")):
            code, out, err = self.cli("inbox", "support", "--json", "--config", config)
        self.assertEqual(code, 0)
        self.assertIn("warning: display_timezone is not available", err)
        self.assertEqual({row["when"] for row in json.loads(out)}, {"11-14 22:13", "11-14 22:14"})

    def test_unreadable_or_invalid_config_returns_2(self):
        code, out, err = self.cli("inbox", "support", "--config", str(self.root / "missing.yaml"))
        self.assertEqual((code, out, err), (2, "", "Invalid input\n"))
        code, _, err = self.cli("inbox", "support", "--config", self.config(people=["UDUMMY1"]))
        self.assertEqual((code, err), (2, "Invalid input\n"))

    def test_when_helper(self):
        self.assertEqual(when(ROOT_TS, timezone.utc), "11-14 22:13")
        for bad in (None, "", "abc", "nan", "inf"):
            self.assertEqual(when(bad, timezone.utc), "-")


class ConfigKeyTests(unittest.TestCase):
    def setUp(self):
        self.raw = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))

    def test_defaults_sample_and_validation(self):
        config = parse_config(self.raw)
        self.assertEqual(config.people, {"U0000000091": "Dummy Person One", "U0000000093": "Dummy Owner"})
        self.assertEqual(config.display_timezone, "UTC")
        self.raw.pop("people")
        self.raw.pop("display_timezone")
        config = parse_config(self.raw)
        self.assertEqual((config.people, config.display_timezone), ({}, "UTC"))
        for key, value in (("people", ["UDUMMY1"]), ("people", {"UDUMMY1": ""}), ("people", {"UDUMMY1": 3}),
                           ("display_timezone", ""), ("display_timezone", 9), ("display_timezone", None)):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ConfigError):
                    parse_config(dict(self.raw, **{key: value}))

    def test_timezone_name_checked_when_a_database_exists(self):
        def lookup(name):
            if name in ("Etc/UTC", "Asia/Seoul"):
                return timezone.utc
            raise ZoneInfoNotFoundError(name)

        with patch("zoneinfo.ZoneInfo", side_effect=lookup):
            self.assertEqual(parse_config(dict(self.raw, display_timezone="Asia/Seoul")).display_timezone,
                             "Asia/Seoul")
            with self.assertRaises(ConfigError):
                parse_config(dict(self.raw, display_timezone="Dummy/Nowhere"))
        with patch("zoneinfo.ZoneInfo", side_effect=ValueError("bad key")):
            with self.assertRaises(ConfigError):
                parse_config(dict(self.raw, display_timezone="../dummy"))
        # Without any database the name cannot be verified and is accepted.
        with patch("zoneinfo.ZoneInfo", side_effect=ZoneInfoNotFoundError("none")):
            self.assertEqual(parse_config(dict(self.raw, display_timezone="Asia/Seoul")).display_timezone,
                             "Asia/Seoul")


if __name__ == "__main__":
    unittest.main()
