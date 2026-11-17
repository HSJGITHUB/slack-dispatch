"""P28 Deliveries content: each row shows what was delivered and who wrote it (dummy ids only)."""
from dataclasses import replace
from pathlib import Path
import re
import shutil
import subprocess
import unittest
import warnings

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - depends on the optional console extra
    raise unittest.SkipTest("console tests need the optional console extra") from None

from slack_dispatch.console import queries
from slack_dispatch.console.app import create_app
from tests.test_console_app import NOW, ConsoleCase, stamp, ts
from tests.test_console_core import seed
from tests.test_console_staging import dictionary_keys

DOMAIN = "dummy-workspace"
STATIC = Path(queries.__file__).with_name("static")
PEOPLE_USER = "U0000000088"        # config people: "Dummy Author"
DIRECTORY_USER = "U0000000301"     # ledger user_directory only
UNKNOWN_USER = "U0000000399"       # no name anywhere
CONFIG_CHANNEL = "C0000000002"     # console.channels: "dummy-general"
MEMBER_CHANNEL = "C0000000311"     # channel_membership only
UNKNOWN_CHANNEL = "C0000000399"
SEEN = "2026-11-13T00:00:00+00:00"
LONG_BODY = "DUMMY  first\n\nline\t" + "x" * 200


class DeliveriesContentTests(ConsoleCase):
    def setUp(self):
        super().setUp()
        self.config = replace(self.config, console=replace(self.config.console,
                                                           workspace_domain=DOMAIN))
        self.client = TestClient(create_app(self.config, self.store, clock=lambda: NOW),
                                 base_url="http://127.0.0.1")
        self.store.put_user_directory(dict(user=DIRECTORY_USER, name="Dummy Directory Person"), SEEN)
        self.store.replace_membership([dict(channel=MEMBER_CHANNEL, kind="public_channel",
                                            name="dummy-member")], SEEN)
        self.people = seed(self.store, ts(10), channel=CONFIG_CHANNEL, user=PEOPLE_USER,
                           text="DUMMY short body", received=stamp(10))
        self.directory = seed(self.store, ts(20), channel=MEMBER_CHANNEL, thread=ts(5),
                              user=DIRECTORY_USER, text=LONG_BODY, received=stamp(20))
        self.unknown = seed(self.store, ts(30), channel=UNKNOWN_CHANNEL, user=UNKNOWN_USER,
                            text=None, received=stamp(30))
        self.store.set_attention(self.directory, "owner_mention")
        self.absent = f"slack:{CONFIG_CHANNEL}:{ts(40)}"  # not in the ledger
        self.deliver("support", [self.people], 30)
        self.deliver("support", [self.directory, self.unknown, self.absent], 20)
        self.store.add_delivery(dict(purpose="human_queue", lane="human_queue", kind="card",
                                     adapter="dispatch_card", status="sent",
                                     item_ids=[self.directory], created_at=stamp(50)))

    def deliver(self, lane, ids, offset):
        self.store.add_delivery(dict(purpose="lane", lane=lane, kind="new", status="sent",
                                     adapter="session_message", item_ids=ids,
                                     created_at=stamp(offset)))

    def link(self, channel, message_ts, thread=None):
        return queries.message_permalink(DOMAIN, channel, message_ts, thread)

    def rows(self, client=None):
        return (client or self.client).get("/api/deliveries?hours=0").json()["rows"]

    def test_item_fields_with_resolved_names_and_permalinks(self):
        card, single, multi = self.rows()
        self.assertEqual(single["item_count"], 1)
        item = single["items"][0]
        for key in ("id", "channel", "channel_name", "author", "author_name", "when", "is_reply",
                    "attention", "preview", "permalink"):
            self.assertIn(key, item)
        self.assertEqual((item["author_name"], item["channel_name"]), ("Dummy Author", "dummy-general"))
        self.assertEqual(item["author"], {"id": PEOPLE_USER, "name": "Dummy Author"})
        self.assertEqual(item["when"], "2026-11-13 00:00")
        self.assertEqual((item["preview"], item["preview_truncated"]), ("DUMMY short body", False))
        self.assertEqual((item["is_reply"], item["attention"]), (False, None))
        self.assertEqual(item["permalink"], self.link(CONFIG_CHANNEL, ts(10)))
        directory, unknown, absent = multi["items"]
        self.assertEqual((directory["author_name"], directory["channel_name"]),
                         ("Dummy Directory Person", "#dummy-member"))
        self.assertEqual((directory["is_reply"], directory["attention"]), (True, "owner_mention"))
        self.assertEqual(directory["permalink"], self.link(MEMBER_CHANNEL, ts(20), ts(5)))
        self.assertEqual((unknown["author_name"], unknown["channel_name"]), (UNKNOWN_USER, UNKNOWN_CHANNEL))
        self.assertEqual(unknown["author"], {"id": UNKNOWN_USER, "name": None})
        self.assertEqual(unknown["preview"], "[no body stored]")
        self.assertTrue(absent["missing"])
        self.assertEqual((absent["preview"], absent["when"], absent["channel_name"]),
                         ("[no body stored]", "2026-11-13 00:00", "dummy-general"))
        self.assertEqual(absent["permalink"], self.link(CONFIG_CHANNEL, ts(40)))
        self.assertNotIn("body", directory)
        self.assertEqual(card["items"][0]["id"], self.directory)
        self.assertEqual(card["items"][0]["author_name"], "Dummy Directory Person")

    def test_preview_collapses_whitespace_and_truncates(self):
        directory = self.rows()[2]["items"][0]
        collapsed = " ".join(LONG_BODY.split())
        self.assertEqual(directory["preview"], collapsed[:120])
        self.assertEqual(len(directory["preview"]), 120)
        self.assertTrue(directory["preview"].startswith("DUMMY first line x"))
        self.assertTrue(directory["preview_truncated"])
        self.assertEqual(queries.delivery_preview("y" * 120), ("y" * 120, False))
        self.assertEqual(queries.delivery_preview("y" * 121), ("y" * 120, True))
        for blank in (None, "", " \n\t "):
            self.assertEqual(queries.delivery_preview(blank), ("[no body stored]", False))

    def test_multi_item_row_keeps_item_order(self):
        multi = self.rows()[2]
        self.assertEqual(multi["item_count"], 3)
        self.assertEqual([item["id"] for item in multi["items"]],
                         [self.directory, self.unknown, self.absent])

    def test_one_ledger_read_for_the_items_of_a_page(self):
        calls = []
        original = self.store._rows

        def counting(query, params=()):
            if query.startswith("SELECT * FROM messages WHERE id IN"):
                calls.append(list(params))
            return original(query, params)

        def refuse(*args, **kwargs):
            raise AssertionError("per-item read")

        self.store._rows = counting
        self.store.get_message = refuse
        try:
            rows = self.rows()
        finally:
            del self.store._rows
            del self.store.get_message
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(calls[0]), sorted([self.people, self.directory, self.unknown,
                                                   self.absent]))

    def test_without_domain_permalinks_are_null(self):
        plain = TestClient(create_app(replace(self.config, console=replace(
            self.config.console, workspace_domain=None)), self.store, clock=lambda: NOW),
            base_url="http://127.0.0.1")
        items = [item for row in self.rows(plain) for item in row["items"]]
        self.assertTrue(items)
        self.assertTrue(all(item["permalink"] is None for item in items))
        self.assertEqual(items[0]["author_name"], "Dummy Directory Person")

    def test_cli_style_row_without_prefetch_still_has_content(self):
        rows = self.store.list_deliveries(lane="support")
        shaped = queries.delivery_row(self.store, queries.View(self.config).bind(self.store),
                                      rows[1], {})
        self.assertEqual([item["preview"] for item in shaped["items"]][:2],
                         [" ".join(LONG_BODY.split())[:120], "[no body stored]"])

    def test_store_without_query_helper_falls_back_per_id(self):
        class Double:
            def __init__(self, store):
                self.store = store

            def get_message(self, message_id, include_body=False):
                return self.store.get_message(message_id, include_body=include_body)

        found = queries.delivery_messages(Double(self.store), [self.people, self.absent, self.people])
        self.assertEqual(list(found), [self.people])
        self.assertEqual(found[self.people]["body"], "DUMMY short body")


class DeliveriesContentFrontEndTests(unittest.TestCase):
    def setUp(self):
        self.script = (STATIC / "console.js").read_text(encoding="utf-8")

    def test_dictionaries_and_labels(self):
        en, ko = dictionary_keys(self.script, "en"), dictionary_keys(self.script, "ko")
        self.assertEqual(en, ko)
        for key in ("deliveryColumns.content", "deliveryFilters.text", "deliveryMoreItems",
                    "deliveryTextPlaceholder", "deliveryTextMatches", "deliveryNoMatch"):
            self.assertIn(key, en)
        korean = self.script[self.script.index("  ko: Object.freeze({"):]
        for label in ('content: "내용"', '"외 " + count + "건"', 'text: "찾기"'):
            self.assertIn(label, korean)
        self.assertIsNone(re.search(chr(0x2014), self.script))

    def test_table_columns_content_cell_and_filter(self):
        self.assertIn("[c.time, c.kind, c.lane, c.content, c.result, c.receipt, c.cost]", self.script)
        for name in ("function deliveryContent(row)", "function deliveryLane(row)",
                     "function renderDeliveryRows()", "item.preview", "item.author_name",
                     "item.channel_name", "item.attention", "item.permalink",
                     'const NO_BODY_STORED = "[no body stored]";'):
            self.assertIn(name, self.script)
        css = (STATIC / "console.css").read_text(encoding="utf-8")
        for rule in (".delivery-preview", "-webkit-line-clamp: 2", "text-overflow: ellipsis",
                     "details.delivery-more"):
            self.assertIn(rule, css)

    def test_script_passes_node_check(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        result = subprocess.run([node, "--check", str(STATIC / "console.js")],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
