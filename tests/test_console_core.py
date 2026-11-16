"""P10 console pieces that need no web framework: config, actions, notices, store reads."""
from dataclasses import replace
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

import yaml

from slack_dispatch.config.loader import ConfigError, load_config, parse_config
from slack_dispatch.config.schema import Console, HumanQueue
from slack_dispatch.console import actions
from slack_dispatch.console.__main__ import bind_address, build_parser, main as console_main
from slack_dispatch.inbox.cli import main as cli_main
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.file_drop import FileDropNotifier
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline
from slack_dispatch.router.classifier import NullSuggester
from tests.test_pipeline_integration import SAMPLE, event, fixture_config

RECEIVED = "2026-11-13T00:00:00+00:00"
ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")


def sample_data():
    return yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))


def seed(store, ts, *, channel="C0000000002", thread=None, user="U0000000088", text=None,
         received=RECEIVED):
    store.record_event(dict(source="slack", event_id="EvDUMMY" + ts, event_type="message",
                            channel=channel, channel_type="channel", user=user, ts=ts,
                            thread_ts=thread, is_thread_reply=bool(thread and thread != ts),
                            has_files=False, received_at=received, text=text))
    return f"slack:{channel}:{ts}"


def open_store(path, lanes=("owner", "support", "content"), store_body=True):
    store = LedgerStore(path, store_body=store_body)
    store.open()
    store.migrate()
    for lane in lanes:
        store.upsert_lane(lane)
    return store


class ConsoleConfigTests(unittest.TestCase):
    def test_defaults_without_section(self):
        data = sample_data()
        del data["console"]
        console = parse_config(data).console
        self.assertEqual(console, Console())
        self.assertEqual((console.host, console.port, console.base_url, console.page_size,
                          console.lane_hint_people, console.direct_lane, console.channels),
                         ("127.0.0.1", 8811, None, 50, True, "owner-direct", {}))
        data["console"] = None
        self.assertEqual(parse_config(data).console, Console())

    def test_sample_section_and_normalised_base_url(self):
        console = load_config(SAMPLE).console
        self.assertEqual((console.direct_lane, console.channels["C0000000001"]), ("owner", "dummy-support"))
        data = sample_data()
        data["console"] = {"base_url": "http://127.0.0.1:8811/", "port": 9001, "page_size": 10,
                           "lane_hint_people": False}
        console = parse_config(data).console
        self.assertEqual((console.base_url, console.port, console.page_size, console.lane_hint_people),
                         ("http://127.0.0.1:8811", 9001, 10, False))

    def test_invalid_values(self):
        bad = [{"port": 0}, {"port": "8811"}, {"port": True}, {"base_url": "ftp://dummy"},
               {"base_url": "http://dummy/?q=1"}, {"base_url": ""}, {"page_size": 0},
               {"page_size": 501}, {"lane_hint_people": "yes"}, {"direct_lane": "missing"},
               {"channels": ["C0000000001"]}, {"channels": {"C0000000001": ""}}, {"host": ""},
               "not-a-map"]
        for value in bad:
            with self.subTest(value=value):
                data = sample_data()
                data["console"] = value
                with self.assertRaises(ConfigError):
                    parse_config(data)


class ConsoleArgumentTests(unittest.TestCase):
    def test_default_bind_is_loopback(self):
        config = load_config(SAMPLE)
        args = build_parser().parse_args(["--config", "dummy.yaml"])
        self.assertIsNone(args.host)
        self.assertEqual(bind_address(args, config), ("127.0.0.1", 8811))
        args = build_parser().parse_args(["--config", "dummy.yaml", "--host", "0.0.0.0",
                                          "--port", "9002"])
        self.assertEqual(bind_address(args, config), ("0.0.0.0", 9002))

    def test_main_exit_codes_without_serving(self):
        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            self.assertEqual(console_main([]), 2)
            self.assertEqual(console_main(["--config", "missing-dummy.yaml"]), 2)
        self.assertIn("Invalid console configuration", stderr.getvalue())
        with tempfile.TemporaryDirectory() as temp:
            data = sample_data()
            data["ledger"]["path"] = str(Path(temp) / "absent.sqlite3")
            path = Path(temp) / "config.yaml"
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            stderr = io.StringIO()
            with patch.object(sys, "stderr", stderr), patch.dict(sys.modules, {"uvicorn": None}):
                self.assertEqual(console_main(["--config", str(path)]), 2)
            self.assertIn("optional dependencies", stderr.getvalue())
            stderr = io.StringIO()
            with patch.object(sys, "stderr", stderr):
                try:
                    import uvicorn  # noqa: F401
                    import fastapi  # noqa: F401
                except ImportError:
                    self.skipTest("console extra (fastapi, uvicorn) is not installed")
                self.assertEqual(console_main(["--config", str(path)]), 2)
            self.assertIn("Ledger not found", stderr.getvalue())
            self.assertFalse((Path(temp) / "absent.sqlite3").exists())


class TwinLedgers(unittest.TestCase):
    """Seed two identical ledgers; the CLI acts on one, the console on the other."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-console-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cli_store = open_store(self.root / "cli.sqlite3")
        self.console_store = open_store(self.root / "console.sqlite3")
        self.addCleanup(self.cli_store.close)
        self.addCleanup(self.console_store.close)
        self.cli_inbox, self.console_inbox = self.root / "cli-inbox", self.root / "console-inbox"
        self.ids = []
        for store in (self.cli_store, self.console_store):
            ids = [seed(store, "1794528000.000100", text="DUMMY_ROOT"),
                   seed(store, "1794528000.000200", thread="1794528000.000100", text="DUMMY_REPLY"),
                   seed(store, "1794528000.000300", text="DUMMY_OTHER")]
            for mid in ids:
                store.set_state(mid, "human_pending", note="none")
            store.mark_followup(ids[1])
            self.ids = ids

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = cli_main(["--inbox-root", str(self.cli_inbox), *args], store=self.cli_store,
                        stdout=out, stderr=err)
        self.assertEqual(code, 0, err.getvalue())

    def rows(self, store):
        result = {}
        for mid in self.ids:
            row = store.get_message(mid)
            for key in ("assigned_at", "ack_at", "done_at"):
                row[key] = row[key] is not None
            result[mid] = row
        return result

    def records(self, root, lane):
        path = root / (lane + ".jsonl")
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        for row in rows:
            self.assertTrue(row.pop("delivered_at"))
        return rows

    def assert_same(self, reason_map=None):
        expected = self.rows(self.cli_store)
        observed = self.rows(self.console_store)
        for mid, row in observed.items():
            if reason_map and row["route_reason"] in reason_map:
                row["route_reason"] = reason_map[row["route_reason"]]
        self.assertEqual(observed, expected)
        for lane in ("owner", "support", "content"):
            self.assertEqual(self.records(self.console_inbox, lane), self.records(self.cli_inbox, lane))


class ActionParityTests(TwinLedgers):
    def test_assign_single_and_bulk_match_cli(self):
        self.cli("assign", self.ids[0], "support", "--note", "dummy note")
        self.cli("assign", self.ids[1], "support")
        self.cli("assign", self.ids[2], "support")
        first = actions.apply(self.console_store, self.console_inbox, "assign", [self.ids[0]],
                              lane="support", note="dummy note", guard=actions.queue_guard)
        bulk = actions.apply(self.console_store, self.console_inbox, "assign", self.ids[1:],
                             lane="support", guard=actions.queue_guard)
        self.assertTrue(all(row["ok"] for row in first + bulk))
        self.assert_same({"human:console": "human:cli"})
        self.assertEqual(self.console_store.get_message(self.ids[1])["route_reason"], "human:console")
        kinds = [row["kind"] for row in self.records(self.console_inbox, "support")]
        self.assertEqual(kinds, ["new", "new", "new"])

    def test_direct_is_assign_to_the_direct_lane(self):
        self.cli("assign", self.ids[0], "owner")
        actions.apply(self.console_store, self.console_inbox, "direct", [self.ids[0]], lane="owner",
                      guard=actions.queue_guard)
        self.assert_same({"human:console": "human:cli"})

    def test_done_ack_and_reroute_match_cli(self):
        self.cli("done", self.ids[0], "--note", "dummy note")
        self.cli("assign", self.ids[1], "support")
        self.cli("ack", self.ids[1])
        self.cli("assign", self.ids[2], "support")
        self.cli("reroute", self.ids[2], "--to", "content")
        store, inbox = self.console_store, self.console_inbox
        actions.apply(store, inbox, "done", [self.ids[0]], note="dummy note", guard=actions.queue_guard)
        actions.apply(store, inbox, "assign", self.ids[1:], lane="support")
        actions.apply(store, inbox, "ack", [self.ids[1]], guard=actions.lane_guard("support"))
        actions.apply(store, inbox, "reroute", [self.ids[2]], lane="content",
                      guard=actions.lane_guard("support"))
        self.assert_same({"human:console": "human:cli"})
        self.assertEqual(store.get_message(self.ids[2])["route_reason"], "reroute:support")
        self.assertEqual([row["kind"] for row in self.records(inbox, "content")], ["reroute"])

    def test_done_without_note_and_exclude_record_console_reason(self):
        self.cli("done", self.ids[0])
        actions.apply(self.console_store, self.console_inbox, "done", [self.ids[0]])
        # The CLI leaves the note unchanged; the console records its provenance.
        self.assertEqual(self.cli_store.get_message(self.ids[0])["note"], "none")
        row = self.console_store.get_message(self.ids[0])
        self.assertEqual((row["state"], row["note"]), ("done", "human:console"))
        result = actions.apply(self.console_store, self.console_inbox, "exclude", [self.ids[2]],
                               guard=actions.queue_guard)
        self.assertTrue(result[0]["ok"])
        row = self.console_store.get_message(self.ids[2])
        self.assertEqual((row["state"], row["note"], row["lane"]), ("excluded", "human:console", None))
        self.assertFalse(self.console_inbox.exists())

    def test_bulk_reports_each_id_and_guards(self):
        missing = "slack:C0000000002:1.000000"
        actions.apply(self.console_store, self.console_inbox, "done", [self.ids[0]])
        results = actions.apply(self.console_store, self.console_inbox, "assign",
                                [self.ids[0], missing, self.ids[2], self.ids[2]], lane="support",
                                guard=actions.queue_guard)
        self.assertEqual([(row["id"], row["ok"], row["error"]) for row in results],
                         [(self.ids[0], False, "conflict"), (missing, False, "not_found"),
                          (self.ids[2], True, None)])
        self.assertEqual(results[0]["detail"], "state is done")
        unknown = actions.apply(self.console_store, self.console_inbox, "assign", [self.ids[1]],
                                lane="missing-lane", guard=actions.queue_guard)
        self.assertEqual(unknown[0]["error"], "lane_not_found")
        self.assertEqual(self.console_store.get_message(self.ids[1])["state"], "human_pending")
        other = actions.apply(self.console_store, self.console_inbox, "ack", [self.ids[2]],
                              guard=actions.lane_guard("content"))
        self.assertEqual((other[0]["error"], other[0]["detail"]), ("conflict", "owned by lane support"))


class NoticeTextTests(unittest.TestCase):
    """Item 6: notices end with the console link only when console.base_url is set."""

    def build(self, base_url=None):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-console-notice-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        config = replace(fixture_config(self.root), human_queue=HumanQueue(digest_seconds=10))
        self.config = replace(config, console=replace(config.console, base_url=base_url))
        self.store = open_store(self.config.ledger.path, store_body=False)
        self.addCleanup(self.store.close)
        self.clock = lambda: 1000.0
        notifiers = [Mock(), Mock(), FileDropNotifier(self.root / "human", {"ids": "{ids}"})]
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(), notifiers,
                                 WakePolicyEngine(self.config.wake_policy, self.clock), self.clock)
        self.pipeline.handle_event(event(user="U0000000091", text="plain"))  # immediate attention
        self.pipeline.handle_event(event(2, text="plain"))
        self.pipeline.handle_event(event(3, text="plain", channel="C0000000003x"))
        self.pipeline.flush(1010)
        drops = [json.loads(path.read_text(encoding="utf-8"))
                 for path in sorted((self.root / "human").glob("*.json"))]
        self.assertEqual(len(drops), 2)
        return {("digest" if "digest" in drop["body"] else "notice"): drop for drop in drops}

    def test_without_base_url_text_is_unchanged(self):
        drops = self.build()
        digest = drops["digest"]["body"].splitlines()
        self.assertTrue(digest[0].startswith("Human queue digest: 2 item(s); channels: "
                                             "C0000000002=1, C0000000003x=1; ids: "))
        # P25 fix1: a digest ends with the operator queue counts (direct lane configured).
        self.assertIn("inbox.cli assign ID LANE", digest[-3])
        self.assertEqual(digest[-2], "Lane ids for assign: owner, support, content")
        self.assertEqual(digest[-1], "Operator queue: undecided=3, direct=0 (lane owner)")
        self.assertNotIn("Open:", drops["digest"]["body"] + drops["notice"]["body"])

    def test_with_base_url_digest_is_short_and_notice_links_message(self):
        drops = self.build("http://127.0.0.1:8811")
        self.assertEqual(drops["digest"]["body"],
                         "Human queue digest: 2 item(s); channels: C0000000002=1, C0000000003x=1\n"
                         "Open: http://127.0.0.1:8811/\n"
                         "Operator queue: undecided=3, direct=0 (lane owner)")  # P25 fix1
        self.assertEqual(len(drops["digest"]["ids"]), 2)  # payload ids are unchanged
        notice = drops["notice"]["body"].splitlines()
        self.assertEqual(notice[-1], "Open: http://127.0.0.1:8811/#msg=slack:C0000000002:1000.000001")
        self.assertEqual(notice[-2], "Lane ids for assign: owner, support, content")
        self.assertIn("; ids: slack:C0000000002:1000.000001", notice[0])


class StoreReadHelperTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-console-store-")
        self.addCleanup(temp.cleanup)
        self.store = open_store(Path(temp.name) / "ledger.sqlite3")
        self.addCleanup(self.store.close)

    def test_thread_search_and_lane_reads(self):
        root = seed(self.store, "1794528000.000100", text="DUMMY 100% root")
        reply = seed(self.store, "1794528000.000900", thread="1794528000.000100", text="DUMMY_x reply")
        early = seed(self.store, "1794528000.000200", thread="1794528000.000100", user="U0000000077",
                     text="DUMMY early")
        seed(self.store, "1794528000.000150", text="unrelated", channel="C0000000009")
        thread = self.store.thread_messages("slack", "C0000000002", "1794528000.000100")
        self.assertEqual([row["id"] for row in thread], [root, early, reply])
        self.assertNotIn("body", thread[0])
        self.assertEqual([row["id"] for row in self.store.search_messages("100%")], [root])
        self.assertEqual([row["id"] for row in self.store.search_messages("Y_x")], [reply])
        self.assertEqual(self.store.search_messages("DUMMY%x"), [])
        self.assertEqual([row["id"] for row in self.store.search_messages("U0000000077")], [early])
        self.assertEqual(len(self.store.search_messages("C0000000002")), 3)
        self.assertEqual(len(self.store.search_messages("C0000000002", limit=2)), 2)
        self.store.set_route(root, "support", "test")
        self.store.set_route(early, "content", "test")
        self.store.set_route(reply, "support", "test")
        counts = self.store.author_lane_counts("U0000000088")
        self.assertEqual([(row["lane"], row["count"]) for row in counts], [("support", 2)])
        self.assertEqual(len(self.store.author_lane_counts("U0000000088", exclude_id=root)), 1)
        self.assertEqual(len(self.store.lane_activity("support")), 2)
        self.assertIsNone(self.store.last_lane_delivery("support"))
        self.assertFalse(self.store.has_table("service_deliveries"))
        self.assertIsNone(self.store.last_lane_reminder("support"))
        self.store.record_gap("service", RECEIVED, note="start")
        self.assertEqual([row["note"] for row in self.store.list_gaps()], ["start"])


@unittest.skipUnless(POWERSHELL, "Windows PowerShell is unavailable")
class ConsoleTaskTemplateTests(unittest.TestCase):
    """The console task mirrors the service task: UTF-16 LE BOM, logon plus minute trigger."""

    def test_render_and_whatif(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            args = ["-Python", r"C:\demo & test\pythonw.exe", "-Workdir", r"C:\demo & test\release",
                    "-Config", r"C:\demo & test\state\config.yaml", "-User", r"DEMO\operator",
                    "-OutputDirectory", str(directory)]
            command = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                       "-File", str(ROOT / "deploy/windows/install_console_task.ps1"), *args]
            preview = subprocess.run(command + ["-WhatIf"], cwd=ROOT, capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=45)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertIn('schtasks.exe /Create /TN "SlackDispatchConsole"', preview.stdout)
            self.assertEqual(list(directory.iterdir()), [])
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr)
            [rendered_path] = list(directory.glob("dispatch-console-task-*.xml"))
            self.assertTrue(rendered_path.read_bytes().startswith(b"\xff\xfe"))
            rendered = rendered_path.read_text(encoding="utf-16")
            self.assertEqual(rendered.splitlines()[0], '<?xml version="1.0" encoding="UTF-16"?>')
            root = ET.fromstring(rendered)
            ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
            self.assertEqual(root.find(".//t:Command", ns).text, args[1])
            self.assertEqual(root.find(".//t:Arguments", ns).text,
                             '-X utf8 -B -m slack_dispatch.console --config "' + args[5] + '"')
            triggers = root.find("t:Triggers", ns)
            for name in ("LogonTrigger", "TimeTrigger"):
                trigger = triggers.find("t:" + name, ns)
                self.assertEqual(trigger.find("t:Repetition/t:Interval", ns).text, "PT1M")
            self.assertEqual(root.find(".//t:MultipleInstancesPolicy", ns).text, "IgnoreNew")
            self.assertEqual(root.find(".//t:UserId", ns).text, args[7])

    def test_template_stays_utf8_like_the_service_template(self):
        template = (ROOT / "deploy/windows/task_template_console.xml").read_bytes()
        self.assertTrue(template.startswith(b'<?xml version="1.0" encoding="UTF-16"?>'))
        service = (ROOT / "deploy/windows/task_template.xml").read_text(encoding="utf-8")
        console = template.decode("utf-8")
        # Normalise line endings: the CI checkout may convert one template and not the other.
        service = service.replace(chr(13) + chr(10), chr(10))
        console = console.replace(chr(13) + chr(10), chr(10))
        self.assertEqual(console.replace("slack_dispatch.console", "slack_dispatch.service")
                         .replace("operator console", "supervision"), service)


if __name__ == "__main__":
    unittest.main()
