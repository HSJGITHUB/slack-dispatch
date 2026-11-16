"""P21 lane session identity, wake now and DM items (dummy ids and dummy titles only)."""
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

import yaml

from slack_dispatch.config.loader import ConfigError, load_config, parse_config
from slack_dispatch.config.schema import Lane
from slack_dispatch.console import queries
from slack_dispatch.inbox import cli
from slack_dispatch.inbox.files import read
from slack_dispatch.notify.session_resolve import (SessionResolver, SessionTable,
                                                   parse_sessions_table)
from tests.test_console_app import DEAD_ID, LIVE_ID, NOW, ConsoleCase, TestClient, stamp, ts
from tests.test_console_core import sample_data, seed
from tests.test_console_staging import dictionary_keys
from slack_dispatch.console.app import create_app

OTHER_ID = "3333dddd-0000-4000-8000-000000000005"
TABLE = f"""# Dummy sessions

| Last used | State | Title | Size | Resume |
| --- | --- | --- | --- | --- |
| 2026-11-15 10:00 | open | Dummy support task | 1.2 MB | `claude --resume {LIVE_ID}` |
| 2026-11-14 09:00 | closed | Dummy content task | 300 KB | `claude --resume {DEAD_ID}` |
| 2026-11-13 08:00 | open | Dummy a \\| b title | 10 KB | `claude --resume {OTHER_ID}` |
| 2026-11-12 08:00 | open | Older duplicate | 1 KB | `claude --resume {LIVE_ID}` |
| 2026-11-12 08:00 | open | No resume command | 1 KB | claude --continue |
| 2026-11-12 08:00 | too few cells |
| 2026-11-12 08:00 | open | Bad id | 1 KB | `claude --resume not-a-uuid` |
not a table row
"""


def write_table(path, text=TABLE):
    path.write_text(text, encoding="utf-8")
    return path


class SessionTableTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-lane-sessions-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_good_and_malformed_rows(self):
        rows = parse_sessions_table(TABLE)
        self.assertEqual(sorted(rows), sorted([LIVE_ID, DEAD_ID, OTHER_ID]))
        self.assertEqual(rows[LIVE_ID], {"id": LIVE_ID, "title": "Dummy support task", "state": "open",
                                         "last_used": "2026-11-15 10:00", "size": "1.2 MB"})
        self.assertEqual(rows[DEAD_ID]["state"], "closed")
        self.assertEqual(rows[OTHER_ID]["title"], "Dummy a | b title")
        self.assertEqual(parse_sessions_table(""), {})
        self.assertEqual(parse_sessions_table(None), {})
        # An empty title cell is None; an upper-case id is indexed in lower case.
        empty = parse_sessions_table(f"| x | open |  | 1 KB | `claude --resume {LIVE_ID.upper()}` |")
        self.assertEqual(empty[LIVE_ID]["title"], None)

    def test_missing_file_and_utf8_titles(self):
        table = SessionTable(self.root / "absent.md")
        self.assertEqual(table.rows(), {})
        self.assertIsNone(table.lookup(LIVE_ID))
        path = write_table(self.root / "sessions.md",
                           f"| x | open | Dummy été title | 1 KB | `claude --resume {LIVE_ID}` |\n")
        table = SessionTable(path)
        self.assertEqual(table.lookup(LIVE_ID[:8])["title"], "Dummy été title")
        self.assertIsNone(table.lookup("dummy-name"))
        self.assertIsNone(table.lookup("ffffffff"))

    def test_prefix_lookup_must_be_unique(self):
        twin = "0000aaaa-1111-4000-8000-000000000009"
        path = write_table(self.root / "sessions.md", TABLE
                           + f"| y | open | Twin | 1 KB | `claude --resume {twin}` |\n")
        table = SessionTable(path)
        self.assertIsNone(table.lookup(LIVE_ID[:8]))
        self.assertEqual(table.lookup(LIVE_ID)["title"], "Dummy support task")
        self.assertEqual(table.lookup("0000aaaa-1111")["title"], "Twin")

    def test_mtime_cache(self):
        path = write_table(self.root / "sessions.md")
        os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        table = SessionTable(path)
        self.assertEqual(table.lookup(LIVE_ID)["title"], "Dummy support task")
        table.lookup(DEAD_ID)
        self.assertEqual(table.parses, 1)
        # Same size and mtime: the cached rows are used even though the text changed.
        path.write_text(TABLE.replace("Dummy support task", "Dummy changed task"), encoding="utf-8")
        os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        self.assertEqual(table.lookup(LIVE_ID)["title"], "Dummy support task")
        self.assertEqual(table.parses, 1)
        os.utime(path, ns=(1_700_000_100_000_000_000, 1_700_000_100_000_000_000))
        self.assertEqual(table.lookup(LIVE_ID)["title"], "Dummy changed task")
        self.assertEqual(table.parses, 2)
        path.unlink()
        self.assertIsNone(table.lookup(LIVE_ID))

    def test_oversized_and_undecodable_files_read_empty(self):
        path = self.root / "sessions.md"
        path.write_bytes(b"\xff\xfe\x00broken")
        self.assertEqual(SessionTable(path).rows(), {})


class DescribeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-describe-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.live("one", LIVE_ID, "dummy-live")
        self.live("two", "4444eeee-0000-4000-8000-000000000006", "dummy-untitled")
        self.table = write_table(self.root / "sessions.md")
        self.resolver = SessionResolver(str(self.sessions), sessions_table=str(self.table))

    def live(self, name, session_id, session_name):
        (self.sessions / (name + ".json")).write_text(
            json.dumps({"sessionId": session_id, "name": session_name}), encoding="utf-8")

    def test_live_with_title(self):
        result = self.resolver.describe(LIVE_ID[:8])
        self.assertEqual(result, {"id8": LIVE_ID[:8], "id": LIVE_ID, "value": LIVE_ID[:8],
                                  "addressed_by": "id", "name": "dummy-live", "live": True,
                                  "title": "Dummy support task", "state": "live",
                                  "table_state": "open"})

    def test_live_without_title(self):
        result = self.resolver.describe("4444eeee")
        self.assertEqual((result["live"], result["name"], result["title"], result["state"],
                          result["id8"]), (True, "dummy-untitled", None, "live", "4444eeee"))
        by_name = self.resolver.describe("dummy-live")
        self.assertEqual((by_name["addressed_by"], by_name["live"], by_name["id"], by_name["title"]),
                         ("name", True, LIVE_ID, "Dummy support task"))

    def test_not_live(self):
        result = self.resolver.describe(DEAD_ID)
        self.assertEqual((result["live"], result["name"], result["title"], result["state"],
                          result["id8"], result["table_state"]),
                         (False, None, "Dummy content task", "not_live", DEAD_ID[:8], "closed"))
        gone = self.resolver.describe("dummy-gone")
        self.assertEqual((gone["live"], gone["id8"], gone["title"], gone["state"]),
                         (False, None, None, "not_live"))
        unknown = self.resolver.describe("99999999")
        self.assertEqual((unknown["title"], unknown["id8"], unknown["state"]),
                         (None, "99999999", "not_live"))

    def test_paused_and_no_session(self):
        paused = self.resolver.describe(LIVE_ID, paused=True)
        self.assertEqual((paused["state"], paused["live"], paused["title"]),
                         ("paused", True, "Dummy support task"))
        empty = self.resolver.describe(None, paused=True)
        self.assertEqual((empty["state"], empty["live"], empty["id8"], empty["value"]),
                         ("no_session", False, None, None))

    def test_ambiguous_and_no_side_effects(self):
        self.live("three", "0000aaaa-2222-4000-8000-000000000007", "dummy-twin")
        result = self.resolver.describe("0000aaaa")
        self.assertEqual((result["state"], result["live"], result["name"]), ("ambiguous", False, None))
        self.assertEqual(self.resolver.lane_state, {})
        self.assertEqual(self.resolver.skipped_no_session, 0)

    def test_without_table(self):
        resolver = SessionResolver(str(self.sessions))
        self.assertIsNone(resolver.table)
        self.assertIsNone(resolver.describe(LIVE_ID)["title"])
        self.assertIsNone(resolver.title(LIVE_ID))
        self.assertEqual(self.resolver.title(DEAD_ID[:8]), "Dummy content task")


class ConfigTests(unittest.TestCase):
    def test_sessions_table_is_optional_string(self):
        data = sample_data()
        self.assertIsNone(parse_config(data).console.sessions_table)
        data["console"] = dict(data["console"] or {}, sessions_table="dummy/sessions.md")
        self.assertEqual(parse_config(data).console.sessions_table, "dummy/sessions.md")
        for bad in ("", 5, ["x"]):
            data["console"]["sessions_table"] = bad
            with self.assertRaises(ConfigError):
                parse_config(data)

    def test_sample_config_documents_the_key(self):
        text = (Path(__file__).resolve().parents[1] / "examples/instance-sample/config.yaml").read_text(
            encoding="utf-8")
        self.assertIn("sessions_table", text)


class LaneSessionCase(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.table = write_table(self.root / "sessions.md")
        self.config = replace(self.config, console=replace(self.config.console,
                                                           sessions_table=str(self.table)))
        self.client = self.make_client()

    def make_client(self, config=None, base_url="http://127.0.0.1"):
        app = create_app(config or self.config, self.store, clock=lambda: NOW)
        return TestClient(app, base_url=base_url)

    def lane_item(self, offset, lane="support", state="assigned", channel="C0000000002"):
        mid = seed(self.store, ts(offset), channel=channel, received=stamp(offset))
        self.store.set_route(mid, lane, "test")
        if state != "assigned":
            self.store.set_state(mid, state)
        return mid

    def wakes(self, lane=None):
        rows = self.store._rows("SELECT * FROM wake_requests ORDER BY id")
        return [row for row in rows if lane is None or row["lane"] == lane]


class LanesPayloadTests(LaneSessionCase):
    def test_lane_cards_carry_identity_and_wake(self):
        self.lane_item(1)
        self.lane_item(2, state="acked")
        self.lane_item(3, state="done")
        cards = {card["lane"]: card for card in self.get("/api/lanes")["lanes"]}
        support = cards["support"]
        self.assertEqual((support["identity"]["title"], support["identity"]["name"],
                          support["identity"]["id8"], support["identity"]["state"]),
                         ("Dummy support task", "dummy-live", LIVE_ID[:8], "live"))
        self.assertEqual(support["wake"], {"allowed": True, "reason": None, "detail": None,
                                           "assigned": 1, "acked": 1})
        # The P10 session object keeps its shape.
        self.assertEqual(set(support["session"]), {"value", "addressed_by", "live", "state", "target"})
        content = cards["content"]
        self.assertEqual((content["identity"]["state"], content["identity"]["title"],
                          content["wake"]["allowed"], content["wake"]["reason"]),
                         ("paused", "Dummy content task", False, "paused"))
        owner = cards["owner"]
        self.assertEqual((owner["identity"]["state"], owner["wake"]["reason"]),
                         ("no_session", "no_session"))

    def test_lane_inbox_header_and_queue_titles(self):
        self.lane_item(1)
        data = self.get("/api/lanes/support/inbox")
        self.assertEqual((data["identity"]["title"], data["wake"]["allowed"]), ("Dummy support task", True))
        self.assertEqual(self.get("/api/lanes/content/inbox")["wake"]["detail"], "Lane is paused")
        queue = self.get("/api/queue")
        self.assertEqual(queue["lane_sessions"]["support"]["title"], "Dummy support task")
        self.assertEqual(queue["lane_sessions"]["owner"]["state"], "no_session")

    def test_deliveries_and_owner_candidates_show_titles(self):
        mid = self.lane_item(1)
        self.store.add_delivery({"created_at": stamp(5), "purpose": "lane", "lane": "support",
                                 "kind": "new", "adapter": "session_message",
                                 "target_session_id": LIVE_ID, "target_session_name": "dummy-live",
                                 "item_count": 1, "item_ids": json.dumps([mid]), "status": "sent"})
        rows = self.get("/api/deliveries?hours=0")["rows"]
        self.assertEqual(rows[0]["target"]["title"], "Dummy support task")

        class Result:
            def as_dict(self):
                return {"candidates": [{"lane": "support", "session": LIVE_ID[:8], "confidence": "x",
                                        "source": "dummy", "evidence": "dummy"},
                                       {"lane": "owner", "session": None, "confidence": "x",
                                        "source": "dummy", "evidence": "dummy"}]}

        class Owner:
            def resolve(self, store, channel, ts, thread_ts):
                return Result()

        resolver = SessionResolver(self.config.sessions_dir, sessions_table=str(self.table))
        data = queries.owner_lookup(self.store, self.config, Owner(), "C0000000002",
                                    titles=queries.session_titles(resolver))
        self.assertEqual([row["session_title"] for row in data["candidates"]],
                         ["Dummy support task", None])
        plain = queries.owner_lookup(self.store, self.config, Owner(), "C0000000002")
        self.assertNotIn("session_title", plain["candidates"][0])

    def test_without_table_payload_shapes_are_unchanged(self):
        config = replace(self.config, console=replace(self.config.console, sessions_table=None))
        client = self.make_client(config)
        mid = self.lane_item(1)
        self.store.add_delivery({"created_at": stamp(5), "purpose": "lane", "lane": "support",
                                 "kind": "new", "adapter": "session_message",
                                 "target_session_id": LIVE_ID, "target_session_name": "dummy-live",
                                 "item_count": 1, "item_ids": json.dumps([mid]), "status": "sent"})
        rows = self.get("/api/deliveries?hours=0", client=client)["rows"]
        self.assertEqual(set(rows[0]["target"]), {"session_id", "short_id", "name"})
        support = {card["lane"]: card for card in self.get("/api/lanes", client=client)["lanes"]}["support"]
        self.assertEqual((support["identity"]["title"], support["identity"]["name"]), (None, "dummy-live"))


class WakeEndpointTests(LaneSessionCase):
    def wake(self, lane, body=None, status=200):
        return self.post(f"/api/lanes/{lane}/wake", body if body is not None else {}, status)

    def test_default_ids_are_assigned_and_acked_items(self):
        assigned = self.lane_item(1)
        acked = self.lane_item(2, state="acked")
        self.lane_item(3, state="done")
        self.lane_item(4, lane="owner")
        context = self.lane_item(5)
        self.store.set_state(context, "assigned", note="context")
        data = self.wake("support")
        self.assertEqual((data["queued"], data["assigned"], data["acked"], data["results"]),
                         (2, 1, 1, []))
        wakes = self.wakes()
        self.assertEqual([(row["lane"], row["message_id"], row["source"], row["kind"]) for row in wakes],
                         [("support", acked, "console", "wake_now"),
                          ("support", assigned, "console", "wake_now")])
        self.assertEqual(data["wake_requests"], [row["id"] for row in wakes])
        records = [row for row in read(str(self.inbox), "support", include_done=True)
                   if row["kind"] == "backfill"]
        self.assertEqual([row["id"] for row in records], [acked, assigned])
        # Message rows do not change.
        self.assertEqual(self.store.get_message(assigned)["state"], "assigned")

    def test_explicit_ids(self):
        mine = self.lane_item(1)
        theirs = self.lane_item(2, lane="owner")
        closed = self.lane_item(3, state="done")
        data = self.wake("support", {"ids": [mine, theirs, closed, "slack:C0000000002:1.0", mine]})
        self.assertEqual(data["queued"], 1)
        self.assertEqual([(row["id"], row["ok"], row["error"]) for row in data["results"]],
                         [(mine, True, None), (theirs, False, "conflict"), (closed, False, "conflict"),
                          ("slack:C0000000002:1.0", False, "not_found")])
        self.assertEqual(data["results"][1]["detail"], "owned by lane owner")
        self.assertEqual([row["message_id"] for row in self.wakes()], [mine])
        self.wake("support", {"ids": []}, 422)

    def test_nothing_to_wake(self):
        data = self.wake("support")
        self.assertEqual((data["queued"], data["undo"], data["journal"]), (0, None, "ok"))
        self.assertEqual(self.store.list_console_actions(10), [])

    def test_paused_refusal(self):
        self.lane_item(1, lane="content")
        response = self.client.post("/api/lanes/content/wake", json={})
        self.assertEqual((response.status_code, response.json()["detail"]), (409, "Lane is paused"))
        self.assertEqual(self.wakes(), [])

    def test_no_session_and_not_live_refusals(self):
        self.lane_item(1, lane="owner")
        response = self.client.post("/api/lanes/owner/wake", json={})
        self.assertEqual((response.status_code, response.json()["detail"]), (409, "Lane has no session"))
        config = replace(self.config, lanes=(Lane("owner", None, False), Lane("support", DEAD_ID, False),
                                             Lane("content", DEAD_ID, True)))
        response = self.make_client(config).post("/api/lanes/support/wake", json={})
        self.assertEqual((response.status_code, response.json()["detail"]),
                         (409, "Lane session is not live"))
        self.assertEqual(self.wakes(), [])

    def test_wake_switch_refusal_and_unknown_lane(self):
        switch = queries.wake_switch_path(self.config)
        switch.parent.mkdir(parents=True, exist_ok=True)
        switch.write_text("", encoding="utf-8")
        response = self.client.post("/api/lanes/support/wake", json={})
        self.assertEqual(response.status_code, 409)
        self.assertIn("switched off", response.json()["detail"])
        self.wake("missing", {}, 400)

    def test_journal_row_and_undo_removes_requests(self):
        first = self.lane_item(1)
        second = self.lane_item(2)
        data = self.wake("support")
        undo = data["undo"]
        self.assertEqual((undo["action"], undo["lane"], undo["count"], data["journal"]),
                         ("lane_wake", "support", 2, "ok"))
        row = self.store.get_console_action(undo["action_id"])
        self.assertEqual((row["action"], row["by"], json.loads(row["ids"])),
                         ("lane_wake", "console", [second, first]))
        self.assertEqual(json.loads(row["before"]), json.loads(row["after"]))
        self.assertEqual(json.loads(row["meta"])["wake_requests"], data["wake_requests"])
        actions = self.get("/api/actions")["actions"]
        self.assertEqual((actions[0]["action"], actions[0]["undoable"]), ("lane_wake", True))
        # The service consumed the first request meanwhile.
        self.store.consume_wake_requests(data["wake_requests"][:1])
        result = self.post(f"/api/undo/{undo['action_id']}", {})
        self.assertEqual((result["counts"]["restored"], result["counts"]["wake_removed"],
                          result["counts"]["wake_sent"]), (2, 1, 1))
        self.assertEqual([row["id"] for row in self.wakes()], data["wake_requests"][:1])
        self.assertEqual(self.store.get_message(first)["state"], "assigned")


class WakeCliTests(LaneSessionCase):
    def config_file(self, **lanes):
        data = sample_data()
        data["lanes"] = [{"id": "owner", "session": None, "paused": False},
                         {"id": "support", "session": LIVE_ID[:8], "paused": False},
                         {"id": "content", "session": DEAD_ID, "paused": True}]
        data["sessions_dir"] = self.config.sessions_dir
        data["ledger"] = dict(data.get("ledger") or {}, path=self.config.ledger.path)
        data["service"] = dict(data.get("service") or {}, state_dir=str(self.root / "state"),
                               log_dir=str(self.root / "logs"))
        data["wake_policy"] = dict(data.get("wake_policy") or {}, state_dir=str(self.root / "state"))
        data["console"] = dict(data.get("console") or {}, sessions_table=str(self.table))
        # Every write location points into the temp directory (never the working directory).
        self.configured_inbox = self.root / "configured-inbox"
        data["inbox"] = {"root": str(self.configured_inbox)}
        data["notify_adapters"] = [dict(adapter, path=str(self.configured_inbox))
                                   if adapter.get("kind") == "file_inbox" else adapter
                                   for adapter in data["notify_adapters"]]
        path = self.root / "dummy-config.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        load_config(path)  # the fixture must be a valid configuration
        return str(path)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(list(argv), store=self.store, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_cli_wake_default_and_json(self):
        config = self.config_file()
        assigned = self.lane_item(1)
        acked = self.lane_item(2, state="acked")
        code, out, err = self.run_cli("--config", config, "--inbox-root", str(self.inbox),
                                      "wake", "support")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("wake support queued=2 assigned=1 acked=1 already_queued=0"))
        self.assertIn("undo: action ", out)
        self.assertEqual([(row["message_id"], row["source"]) for row in self.wakes()],
                         [(acked, "cli"), (assigned, "cli")])
        action = self.store.list_console_actions(1)[0]
        self.assertEqual((action["action"], action["by"]), ("lane_wake", "cli"))
        code, out, _ = self.run_cli("--config", config, "--inbox-root", str(self.inbox),
                                    "wake", "support", assigned, "--json")
        data = json.loads(out)
        # fix1: the item still has an unconsumed request, so it is not queued again.
        self.assertEqual((code, data["queued"], data["already_queued"], data["results"][0]["detail"]),
                         (0, 0, 1, "already_queued"))
        self.store.consume_wake_requests([row["id"] for row in self.wakes()])
        code, out, _ = self.run_cli("--config", config, "--inbox-root", str(self.inbox),
                                    "wake", "support", assigned, "--json")
        data = json.loads(out)
        self.assertEqual((code, data["queued"], data["records_written"], data["records_skipped"]),
                         (0, 1, 0, 1))
        code, out, err = self.run_cli("--config", config, "--inbox-root", str(self.inbox),
                                      "wake", "support", "slack:C0000000002:1.0")
        self.assertEqual(code, 3)
        self.assertIn("slack:C0000000002:1.0 not_found", out)

    def test_cli_wake_uses_configured_inbox_and_cli_undo(self):
        config = self.config_file()
        mid = self.lane_item(1)
        env = {key: os.environ.pop(key) for key in ("SLACK_DISPATCH_INBOX",) if key in os.environ}
        self.addCleanup(os.environ.update, env)
        code, _, _ = self.run_cli("--config", config, "wake", "support")
        self.assertEqual(code, 0)
        configured = queries.inbox_root(load_config(config))
        self.assertEqual(Path(configured), self.configured_inbox)
        self.assertEqual([row["id"] for row in read(configured, "support") if row["kind"] == "backfill"],
                         [mid])
        self.assertFalse((self.inbox / "support.jsonl").exists())
        # The wake journal uses WAKE_CLOCK and the CLI undo UNDO_CLOCK (both the real clock).
        code, out, _ = self.run_cli("undo", "--last")
        self.assertEqual(code, 0)
        self.assertIn("wake_removed 1", out)
        self.assertEqual(self.wakes(), [])

    def test_cli_wake_refusals(self):
        config = self.config_file()
        self.lane_item(1, lane="content")
        code, _, err = self.run_cli("--config", config, "wake", "content")
        self.assertEqual((code, err.strip()), (1, "Wake refused: Lane is paused"))
        code, _, err = self.run_cli("--config", config, "wake", "owner")
        self.assertEqual((code, err.strip()), (1, "Wake refused: Lane has no session"))
        code, _, err = self.run_cli("--config", config, "wake", "missing")
        self.assertEqual((code, err.strip()), (3, "Lane not found"))
        env = {key: os.environ.pop(key) for key in ("SLACK_DISPATCH_CONFIG",) if key in os.environ}
        self.addCleanup(os.environ.update, env)
        code, _, err = self.run_cli("wake", "support")
        self.assertEqual(code, 2)
        self.assertIn("Configuration is required", err)
        self.assertEqual(self.wakes(), [])


class DmItemTests(LaneSessionCase):
    def dm_pending(self, offset):
        self.store.record_event(dict(source="slack", event_id="EvDUMMYDM" + str(offset),
                                     event_type="message", channel="D0000000001", channel_type="im",
                                     user="U0000000088", ts=ts(offset), thread_ts=None,
                                     is_thread_reply=False, has_files=False,
                                     received_at=stamp(offset), text="dummy dm"))
        mid = f"slack:D0000000001:{ts(offset)}"
        self.store.set_state(mid, "human_pending", note="none")
        return mid

    def test_queue_rows_flag_direct_messages(self):
        dm = self.dm_pending(1)
        channel = self.pending(2)
        items = {item["id"]: item for item in self.get("/api/queue")["items"]}
        self.assertTrue(items[dm]["is_dm"])
        self.assertFalse(items[channel]["is_dm"])

    def test_scope_tab_dm_partner(self):
        self.store.replace_membership([{"channel": "D0000000001", "kind": "im",
                                        "partner_user": "U0000000088", "is_member": True}], stamp(0))
        self.store.add_scope_claim("support", "dm", "D0000000001")
        self.store.record_scope_evidence("dm", "D0000000001", "support", "scope", min_evidence=2)
        self.store.add_scope_claim("support", "channel", "C0000000002")
        self.store.record_scope_evidence("channel", "C0000000002", "support", "scope", min_evidence=2)
        self.store.confirm_scope_claim("content", "dm", "D0000000009", by="console")
        data = self.get("/api/proposals")
        rows = {row["kind"]: row for row in data["proposals"]}
        self.assertEqual(rows["dm"]["partner"], {"id": "U0000000088", "name": "Dummy Author"})
        self.assertEqual(rows["dm"]["name"], "DM:Dummy Author")
        self.assertIsNone(rows["channel"]["partner"])
        learned = {row["key"]: row for row in data["learned"]}
        self.assertEqual(learned["D0000000009"]["partner"], {"id": None, "name": None})


class FrontEndTests(unittest.TestCase):
    def test_dictionaries_and_wiring(self):
        script = (Path(queries.__file__).with_name("static") / "console.js").read_text(encoding="utf-8")
        english, korean = dictionary_keys(script, "en"), dictionary_keys(script, "ko")
        for key in ("sessionIs", "badgeNoSession", "badgePaused", "badgeSessionOff", "wakeNow",
                    "wakeItem", "wakeQueued", "dmBadge", "dmProposalsTitle", "dmClaimsTitle",
                    "laneButtonTitle", "journalActions.lane_wake", "columns.partner"):
            self.assertIn(key, english)
            self.assertIn(key, korean)
        for label in ('wakeNow: "지금 깨우기"', 'wakeItem: "이 항목 깨우기"', 'badgeNoSession: "세션 없음"',
                      'badgePaused: "일시 정지"', 'badgeSessionOff: "세션 꺼짐"',
                      'wakeQueued: (count) => "깨우기 요청 " + count + "건"'):
            self.assertIn(label, script)
        for wiring in ('"/wake"', "item.is_dm", "state.laneSessions[entry.lane]", "row.session_title",
                       "row.target.title", "UI.dmProposalsTitle", "UI.dmClaimsTitle",
                       'last.action === "lane_wake"'):
            self.assertIn(wiring, script)
        self.assertNotIn("\u2014", script)


if __name__ == "__main__":
    unittest.main()
