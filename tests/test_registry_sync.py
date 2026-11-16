"""P17 registry sync: console registry badges, registry_closed staging data, GET /api/owner,
the commit note, and the `registry check` CLI (dummy ids only)."""
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest

import yaml

from slack_dispatch.config.schema import OwnerResolver as OwnerSettings
from slack_dispatch.console import queries
from slack_dispatch.inbox.cli import main as cli_main
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.resolver import OwnerResolver, RegistryIndex, registry_info
from tests.test_console_app import ConsoleCase, ts
from tests.test_console_staging import STATIC, dictionary_keys
from tests.test_pipeline_integration import SAMPLE

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "registry_sample.json"
NEWER_NOTE = "registry:closed:DUMMY-CLOSED-NEWER"


def enabled(path=FIXTURE):
    return OwnerSettings(enabled=True, registry_path=str(path))


class RegistryInfoTests(unittest.TestCase):
    def setUp(self):
        self.index = RegistryIndex.parse(json.loads(FIXTURE.read_text(encoding="utf-8")))

    def info(self, message_ts, thread=None, channel="C0000000002"):
        return registry_info(self.index.match(channel, message_ts, thread), message_ts)

    def test_registry_closed_rule_newer_older_and_none(self):
        newer = self.info(ts(200))
        self.assertEqual((newer["closed"], newer["last_ts"], newer["last_ts_newer"],
                          newer["registry_closed"], newer["note"]),
                         (True, ts(300), True, True, NEWER_NOTE))
        equal = self.info(ts(300), thread=ts(200))
        self.assertTrue(equal["registry_closed"])  # last_ts at the message ts still counts
        older = self.info("1794528350.000100", thread=ts(200))
        self.assertEqual((older["last_ts_newer"], older["registry_closed"], older["note"]),
                         (False, False, None))
        no_ts = self.info(ts(400))
        self.assertEqual((no_ts["closed"], no_ts["last_ts"], no_ts["last_ts_newer"],
                          no_ts["registry_closed"]), (True, None, None, False))
        still_open = self.info(ts(100))
        self.assertEqual((still_open["closed"], still_open["registry_closed"], still_open["owner"]),
                         (False, False, "Dummy Person (session 0000aaaa)"))
        self.assertIsNone(self.info(ts(5)))
        self.assertEqual(set(newer), {"id", "title", "status", "owner", "waiting_on", "next_action",
                                      "closed", "last_ts", "last_ts_newer", "registry_closed", "note"})


class RegistryFix1Tests(unittest.TestCase):
    def test_summary_clips_free_text_to_160(self):
        long = "x" * 400
        data = {"threads": [{"id": "DUMMY-LONG", "ch": "C0DUMMY0001", "root_ts": ts(1), "status": "open",
                             "title": long, "owner": long, "next_action": long, "waiting_on": long}]}
        summary = RegistryIndex.parse(data).rows[0].summary()
        for key in ("title", "owner", "next_action", "waiting_on"):
            self.assertEqual(len(summary[key]), 160)
            self.assertTrue(summary[key].endswith("..."))
        short = RegistryIndex.parse(json.loads(FIXTURE.read_text(encoding="utf-8"))).match(
            "C0000000002", ts(100)).summary()
        self.assertEqual(short["next_action"], "dummy next step")

    def test_close_note_without_registry_id(self):
        data = {"threads": [{"ch": "C0DUMMY0001", "root_ts": ts(1), "status": "closed",
                             "last_out_ts": ts(2)},
                            {"ch": "C0DUMMY0001", "root_ts": ts(3), "status": "closed", "id": "",
                             "last_out_ts": ts(4)}]}
        index = RegistryIndex.parse(data)
        for root, last in ((ts(1), ts(2)), (ts(3), ts(4))):
            info = registry_info(index.match("C0DUMMY0001", root), root)
            self.assertTrue(info["registry_closed"])
            self.assertEqual(info["note"], f"registry:closed:C0DUMMY0001:{root}")
            self.assertNotIn("None", info["note"])
        self.assertEqual(registry_info(index.match("C0DUMMY0001", ts(1)), ts(1))["id"], None)

    def test_row_button_follows_every_poll(self):
        script = (STATIC / "console.js").read_text(encoding="utf-8")
        render = script[script.index("function renderQueue("):script.index("function laneOrder(")]
        self.assertIn("paintRegistry(card, item);", render)
        painter = script[script.index("function paintRegistry("):]
        self.assertIn("state.registryClosed.find(", painter[:1500])
        self.assertIn("clear(card.registrySlot)", painter[:1500])
        self.assertNotIn("item.registry.registry_closed\n    ? el(", script)


class ConsoleRegistryTests(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.plain = self.client
        self.config = replace(self.config, owner_resolver=enabled())
        self.client = self.make_client()

    def test_queue_badges_and_registry_closed_list(self):
        newer = self.pending(200)
        older = self.pending(350, thread=200)
        no_ts = self.pending(400)
        still_open = self.pending(100)
        closed_older = self.pending(450)
        unrelated = self.pending(5)
        data = self.get("/api/queue")
        self.assertEqual(data["registry_closed"], [{"id": newer, "note": NEWER_NOTE}])
        items = {item["id"]: item["registry"] for item in data["items"]}
        self.assertEqual((items[newer]["status"], items[newer]["registry_closed"]), ("closed", True))
        self.assertEqual((items[older]["id"], items[older]["registry_closed"], items[older]["last_ts_newer"]),
                         ("DUMMY-CLOSED-NEWER", False, False))
        self.assertEqual((items[no_ts]["status"], items[no_ts]["registry_closed"]), ("answered", False))
        self.assertEqual((items[still_open]["closed"], items[still_open]["next_action"]),
                         (False, "dummy next step"))
        self.assertEqual((items[closed_older]["status"], items[closed_older]["registry_closed"]),
                         ("Delivered", False))
        self.assertIsNone(items[unrelated])
        # Nothing was written by the listing.
        self.assertEqual(self.store.get_message(newer)["state"], "human_pending")
        page = self.get("/api/queue?limit=1&offset=5")
        self.assertEqual((len(page["items"]), page["registry_closed"]), (1, data["registry_closed"]))

    def test_without_resolver_rows_carry_no_registry(self):
        self.pending(200)
        data = self.get("/api/queue", client=self.plain)
        self.assertEqual((data["registry_closed"], data["items"][0]["registry"]), ([], None))
        self.assertFalse(self.get("/api/config", client=self.plain)["owner_resolver"])
        self.assertTrue(self.get("/api/config")["owner_resolver"])
        self.get("/api/owner?channel=C0000000002", 404, client=self.plain)

    def test_commit_writes_the_registry_note_only_on_go(self):
        newer = self.pending(200)
        other = self.pending(5)
        staged = {"id": newer, "action": "done", "note": NEWER_NOTE}
        self.assertEqual(self.store.get_message(newer)["state"], "human_pending")
        data = self.post("/api/queue/commit", {"decisions": [staged]})
        self.assertEqual(data["counts"], {"applied": 1, "conflict": 0, "not_found": 0, "error": 0})
        row = self.store.get_message(newer)
        self.assertEqual((row["state"], row["note"]), ("done", NEWER_NOTE))
        queue = self.get("/api/queue")
        self.assertEqual((queue["pending_ids"], queue["registry_closed"]), ([other], []))

    def test_api_owner_shape(self):
        reply = self.pending(150, thread=100)
        data = self.get("/api/owner?channel=C0000000002&ts=" + ts(150))
        self.assertEqual(set(data), {"channel", "ts", "thread_ts", "root", "best", "candidates",
                                     "registry_row", "lanes"})
        self.assertEqual((data["ts"], data["thread_ts"], data["root"]), (ts(150), ts(100), ts(100)))
        self.assertEqual(data["best"], {"lane": "support", "confidence": "declared", "source": "registry",
                                        "evidence": data["candidates"][0]["evidence"]})
        self.assertEqual(set(data["candidates"][0]), {"lane", "session", "confidence", "source", "evidence"})
        self.assertEqual(data["registry_row"]["id"], "DUMMY-ROOT-OPEN")
        self.assertEqual(data["lanes"], ["owner", "support", "content"])
        self.assertEqual(self.store.get_message(reply)["state"], "human_pending")
        channel_only = self.get("/api/owner?channel=C0DUMMY0006")
        self.assertEqual((channel_only["root"], channel_only["best"]["lane"], channel_only["best"]["source"]),
                         (None, "content", "routing_rule"))
        self.get("/api/owner?channel=not-a-channel", 400)
        self.get("/api/owner?channel=C0000000002&ts=abc", 400)
        self.get("/api/owner", 422)

    def test_lane_inbox_rows_carry_registry(self):
        root = self.pending(200)
        reply = self.pending(350, thread=200)
        self.post("/api/queue/actions", {"action": "assign", "ids": [root, reply], "lane": "support"})
        groups = self.get("/api/lanes/support/inbox")["groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual((groups[0]["registry"]["id"], groups[0]["registry"]["registry_closed"]),
                         ("DUMMY-CLOSED-NEWER", True))
        self.assertEqual([(item["id"], item["registry"]["registry_closed"], item["channel"])
                          for item in groups[0]["items"]], [(reply, False, "C0000000002")])
        plain = self.get("/api/lanes/support/inbox", client=self.plain)["groups"]
        self.assertIsNone(plain[0]["registry"])

    def test_queue_page_query_directly(self):
        newer = self.pending(200)
        owner = OwnerResolver(self.config)
        view = queries.View(self.config)
        data = queries.queue_page(self.store, self.config, view, 0, owner=owner)
        self.assertEqual(data["registry_closed"], [{"id": newer, "note": NEWER_NOTE}])
        self.assertEqual(queries.queue_page(self.store, self.config, view, 0)["registry_closed"], [])


class ConsoleTextTests(unittest.TestCase):
    def test_strings_exist_in_both_languages(self):
        script = (STATIC / "console.js").read_text(encoding="utf-8")
        for key in ("registryBadge", "registryDone", "registryDoneAll", "findOwner", "ownerLookupTitle",
                    "columns.confidence", "columns.session"):
            self.assertIn(key, dictionary_keys(script, "en"))
            self.assertIn(key, dictionary_keys(script, "ko"))
        for text in ("레지스트리대로 끝 예약",
                     "레지스트리 종결 전부 예약 (",
                     "소관 찾기", "\"Find owner\"", "/api/owner?channel="):
            self.assertIn(text, script)


class RegistryCheckCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-registry-cli-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        self.data["owner_resolver"] = {"enabled": False, "registry_path": str(FIXTURE)}
        self.config_path = self.write_config(self.data)
        self.store = LedgerStore(self.root / "ledger.sqlite3")
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in ("owner", "support", "content"):
            self.store.upsert_lane(lane)

    def write_config(self, data, name="config.yaml"):
        path = self.root / name
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return path

    def message(self, message_ts, state, *, thread=None, lane=None):
        self.store.record_event(dict(source="slack", event_id="EvDUMMY" + message_ts, event_type="message",
                                     channel="C0000000002", user="U0000000088", ts=message_ts,
                                     thread_ts=thread, received_at="2026-11-13T00:00:00+00:00"))
        mid = f"slack:C0000000002:{message_ts}"
        if lane:
            self.store.set_route(mid, lane, "test", state=state)
        else:
            self.store.set_state(mid, state)
        return mid

    def run_cli(self, *argv, config=None):
        out, err = io.StringIO(), io.StringIO()
        code = cli_main(list(argv) + ["--config", str(config or self.config_path)],
                        store=self.store, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_lists_open_items_whose_registry_row_is_closed(self):
        newer = self.message(ts(200), "human_pending")
        older = self.message("1794528350.000100", "assigned", thread=ts(200), lane="support")
        no_ts = self.message(ts(400), "acked", lane="content")
        self.message(ts(250), "done", thread=ts(200), lane="support")  # not open
        self.message(ts(100), "human_pending")  # registry row still open
        code, out, _ = self.run_cli("registry", "check")
        lines = out.splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], "id state lane channel thread registry_id status last_ts message_ts compare")
        self.assertEqual(lines[1:4], [
            f"{newer} human_pending - C0000000002 - DUMMY-CLOSED-NEWER closed {ts(300)} {ts(200)} newer",
            f"{older} assigned support C0000000002 {ts(200)} DUMMY-CLOSED-NEWER closed {ts(300)} "
            f"1794528350.000100 older",
            f"{no_ts} acked content C0000000002 - DUMMY-CLOSED-NOTS answered - {ts(400)} unknown"])
        self.assertTrue(lines[4].startswith("closed_in_registry=3 registry_closed=1"))
        code, out, _ = self.run_cli("registry", "check", "--lane", "support", "--json")
        data = json.loads(out)
        self.assertEqual((code, data["count"], data["rows"][0]["id"], data["rows"][0]["compare"]),
                         (0, 1, older, "older"))
        self.assertEqual(self.store.get_message(newer)["state"], "human_pending")  # no write

    def test_requires_config_and_registry_path(self):
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(cli_main(["registry", "check"], store=self.store, stdout=out, stderr=err), 2)
        data = dict(self.data, owner_resolver={"enabled": True})
        code, _, err = self.run_cli("registry", "check", config=self.write_config(data, "bare.yaml"))
        self.assertEqual((code, err.strip()), (2, "owner_resolver.registry_path is not configured"))


if __name__ == "__main__":
    unittest.main()
