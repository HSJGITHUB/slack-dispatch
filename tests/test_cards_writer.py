"""P12 items 2 and 6: dispatch_card requests for immediate human-queue notices."""
from dataclasses import replace
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from slack_dispatch.config.loader import ConfigError, parse_config
from slack_dispatch.config.schema import Lane, NotifyAdapter
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify import build_notifiers
from slack_dispatch.notify import dispatch_card
from slack_dispatch.notify.dispatch_card import DispatchCardNotifier, card_lanes, safe_name
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline
from slack_dispatch.router.classifier import NullSuggester
from tests.test_pipeline_integration import SAMPLE, event, fixture_config

CARD_KEYS = {"schema", "message_id", "title", "body", "lanes", "suggested_lane", "attention",
             "permalink", "created_at"}
OWNER = "U0000000093"


def card_config(root, *, mode="immediate", only_immediate=True, base_url=None, domain="example"):
    config = fixture_config(root)
    adapters = (config.notify_adapters[0],
                replace(config.notify_adapters[2], path=str(root / "human")),
                NotifyAdapter("dispatch_card", str(root / "cards_dir"), purpose="human_queue",
                              only_immediate=only_immediate, workspace_domain=domain))
    return replace(config, notify_adapters=adapters,
                   human_queue=replace(config.human_queue, mode=mode),
                   console=replace(config.console, base_url=base_url))


class CardCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-cards-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = 1000.0

    def build(self, **options):
        self.config = card_config(self.root, **options)
        self.store = LedgerStore(self.config.ledger.path, store_body=True)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id, session_pointer=lane.session, paused=lane.paused)
        notifiers = build_notifiers(self.config, self.store)
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(), notifiers,
                                 WakePolicyEngine(self.config.wake_policy, lambda: self.now),
                                 lambda: self.now)
        return self.pipeline

    def cards(self):
        folder = self.root / "cards_dir" / "cards"
        return sorted(folder.glob("*.json")) if folder.exists() else []

    def human_notices(self):
        return sorted((self.root / "human").glob("*.json")) if (self.root / "human").exists() else []


class CardShapeTests(CardCase):
    def test_owner_mention_writes_one_card_in_the_p11_shape(self):
        self.build()
        body = f"<@{OWNER}> DUMMY_PRIVATE_BODY " + "x" * 2000
        with self.assertLogs("slack_dispatch", level="INFO") as logs:
            result = self.pipeline.handle_event(event(text=body))
        self.assertEqual(result.state, "human_pending")
        [path] = self.cards()
        self.assertEqual(path.name, safe_name(result.message_id) + ".json")
        self.assertEqual(path.name, "slack_C0000000002_1000_000001.json")
        card = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(card), CARD_KEYS)
        self.assertEqual(card["schema"], "dispatch_card/1")
        self.assertEqual(card["message_id"], result.message_id)
        self.assertEqual(card["title"], "[slack-dispatch] owner mention dummy-general")
        self.assertEqual(card["body"], body[:1500])
        # The sample direct lane (owner) has the fixed `direct` button, not a lane button.
        self.assertEqual(card["lanes"], ["content", "support"])
        self.assertIsNone(card["suggested_lane"])
        self.assertEqual(card["attention"], "owner_mention")
        self.assertEqual(card["permalink"], "https://example.slack.com/archives/C0000000002/p1000000001")
        self.assertIsNotNone(datetime.fromisoformat(card["created_at"]).tzinfo)
        self.assertLessEqual(len(card["created_at"]), 40)
        self.assertEqual(list((self.root / "cards_dir/cards/.tmp").iterdir()), [])
        # The existing human adapter keeps working alongside the card adapter.
        self.assertEqual(len(self.human_notices()), 1)
        self.assertNotIn("DUMMY_PRIVATE_BODY", "\n".join(logs.output))

    def test_body_falls_back_to_the_ids_only_summary_and_carries_the_console_link(self):
        self.build(base_url="http://127.0.0.1:8811", domain=None)
        self.store.store_body = False
        result = self.pipeline.handle_event(event(text=f"<@{OWNER}> urgent"))
        self.assertEqual(result.state, "human_pending")
        card = json.loads(self.cards()[0].read_text(encoding="utf-8"))
        self.assertTrue(card["body"].startswith("Human queue notice: 1 item(s)"))
        self.assertTrue(card["body"].endswith("Open: http://127.0.0.1:8811/#msg=" + result.message_id))
        self.assertEqual(card["body"].count("Open: "), 1)
        self.assertIsNone(card["permalink"])
        self.assertEqual(card["lanes"], ["content", "support"])

    def test_cards_only_for_human_pending_attention_items(self):
        self.build()
        assigned = self.pipeline.handle_event(event(1, text=f"[support] <@{OWNER}> fyi"))
        self.assertEqual(assigned.state, "assigned")
        self.assertEqual(self.cards(), [])
        # The assignment FYI still reaches the existing human-queue adapter.
        self.assertEqual(len(self.human_notices()), 1)
        self.assertEqual(self.store._rows("SELECT adapter FROM human_notices WHERE adapter=2"), [])
        pending = self.pipeline.handle_event(event(2, text=f"<@{OWNER}> urgent"))
        self.assertEqual(pending.state, "human_pending")
        [path] = self.cards()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["message_id"], pending.message_id)

    def test_stored_body_with_console_link_stays_within_1500_characters(self):
        config = card_config(self.root, base_url="http://127.0.0.1:8811")
        body = dispatch_card.card_body("y" * 3000, "summary", "http://127.0.0.1:8811/#msg=slack:C1:1.2")
        self.assertEqual(len(body), 1500)
        self.assertTrue(body.endswith("\n\nOpen: http://127.0.0.1:8811/#msg=slack:C1:1.2"))
        self.assertEqual(dispatch_card.console_link(config, "slack:C1:1.2"),
                         "http://127.0.0.1:8811/#msg=slack:C1:1.2")
        self.assertEqual(dispatch_card.card_body(None, "summary only", None), "summary only")

    def test_digests_and_ordinary_items_produce_no_card(self):
        self.build(mode="digest")
        self.pipeline.handle_event(event(1, text="ordinary pending item"))
        self.assertEqual(self.store._rows("SELECT adapter FROM human_notices"), [{"adapter": 1}])
        self.now += 4000
        self.pipeline.flush(self.now)
        self.assertEqual(self.cards(), [])
        self.assertEqual(len(self.human_notices()), 1)
        self.pipeline.handle_event(event(2, text=f"<@{OWNER}> urgent"))
        self.assertEqual(len(self.cards()), 1)
        rows = self.store._rows("SELECT * FROM human_notices WHERE adapter=2")
        self.assertEqual(self.pipeline.human._send(2, rows, self.now, True), 0)

    def test_only_immediate_false_cards_every_per_item_notice(self):
        self.build(only_immediate=False)
        self.pipeline.handle_event(event(1, text="ordinary pending item"))
        self.assertEqual(len(self.cards()), 1)

    def test_ids_only_notify_is_a_no_op(self):
        self.build()
        notifier = self.pipeline.notifiers[2]
        self.assertIsInstance(notifier, DispatchCardNotifier)
        self.assertIsNone(notifier.notify("human_queue", ["slack:C0000000002:1000.000001"]))
        self.assertEqual(self.cards(), [])


class LaneOrderTests(unittest.TestCase):
    class Bindings:
        def lookup_binding(self, source, channel, thread_ts):
            return "gamma" if (source, channel, thread_ts) == ("slack", "C0000000009", "10.0") else None

    def test_suggestion_then_channel_default_then_binding_then_alphabetical_capped_at_five(self):
        config = fixture_config(Path(tempfile.gettempdir()))
        ids = ("zeta", "eta", "delta", "gamma", "beta", "alpha", "human_queue", "direct", "-bad")
        config = replace(config, lanes=tuple(Lane(lane, None, False) for lane in ids),
                         channel_defaults={"C0000000009": "beta"})
        message = {"source": "slack", "channel": "C0000000009", "ts": "10.5", "thread_ts": "10.0",
                   "classifier_suggestion": "zeta"}
        self.assertEqual(card_lanes(config, self.Bindings(), message),
                         ["zeta", "beta", "gamma", "alpha", "delta"])
        self.assertEqual(card_lanes(config, self.Bindings(), {"channel": "C0000000001", "ts": "1.0"}),
                         ["alpha", "beta", "delta", "eta", "gamma"])

    def test_direct_lane_is_left_out_and_its_slot_goes_to_the_next_lane(self):
        config = fixture_config(Path(tempfile.gettempdir()))
        ids = ("zeta", "eta", "delta", "gamma", "beta", "alpha")
        config = replace(config, lanes=tuple(Lane(lane, None, False) for lane in ids))
        message = {"channel": "C0000000001", "ts": "1.0", "classifier_suggestion": "alpha"}
        direct = replace(config, console=replace(config.console, direct_lane="alpha"))
        self.assertEqual(card_lanes(direct, self.Bindings(), message),
                         ["beta", "delta", "eta", "gamma", "zeta"])
        unset = replace(config, console=replace(config.console, direct_lane=None))
        self.assertEqual(card_lanes(unset, self.Bindings(), message),
                         ["alpha", "beta", "delta", "eta", "gamma"])

    def test_suggested_lane_is_set_only_when_offered(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-cards-")
        self.addCleanup(temp.cleanup)
        config = card_config(Path(temp.name))
        store = LedgerStore(Path(temp.name) / "ledger.sqlite3")
        store.open()
        store.migrate()
        self.addCleanup(store.close)
        mid = store.record_event(event()).message_id
        store.set_suggestion(mid, "support", "dummy", "v1")
        card = dispatch_card.build_card(config, store, mid, "summary", workspace_domain=None)
        self.assertEqual((card["lanes"][0], card["suggested_lane"]), ("support", "support"))
        store.set_suggestion(mid, "unknown-lane", "dummy", "v1")
        card = dispatch_card.build_card(config, store, mid, "summary")
        self.assertIsNone(card["suggested_lane"])


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-cards-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.card = {"schema": "dispatch_card/1", "message_id": "slack:C0000000001:1.000100",
                     "title": "t", "body": "b", "lanes": ["support"], "suggested_lane": None,
                     "attention": None, "permalink": None, "created_at": "2026-01-01T00:00:00+00:00"}

    def test_card_is_written_to_tmp_part_then_renamed(self):
        real = os.replace
        seen = []

        def checked(source, target):
            source, target = Path(source), Path(target)
            seen.append((source, target))
            self.assertEqual(source.parent, self.root / "cards" / ".tmp")
            self.assertTrue(source.name.startswith("slack_C0000000001_1_000100."))
            self.assertEqual(source.suffix, ".part")
            self.assertFalse(target.exists())
            self.assertEqual(json.loads(source.read_text(encoding="utf-8")), self.card)
            return real(source, target)

        with patch.object(dispatch_card.os, "replace", side_effect=checked):
            target = dispatch_card.write_card(self.root, self.card)
        self.assertEqual(target, self.root / "cards" / "slack_C0000000001_1_000100.json")
        self.assertEqual(len(seen), 1)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), self.card)

    def test_failed_rename_exposes_no_card(self):
        with patch.object(dispatch_card.os, "replace", side_effect=OSError("dummy")):
            with self.assertRaises(OSError):
                dispatch_card.write_card(self.root, self.card)
        self.assertEqual(list((self.root / "cards").glob("*.json")), [])


class CardRetryTests(CardCase):
    def test_write_failure_is_retried_by_the_human_queue(self):
        pipeline = self.build()
        with patch.object(dispatch_card.os, "replace", side_effect=OSError("dummy")), \
                self.assertLogs("slack_dispatch.pipeline", level="ERROR"):
            pipeline.handle_event(event(text=f"<@{OWNER}> urgent"))
        self.assertEqual(self.cards(), [])
        pipeline.flush(1001.0)
        self.assertEqual(len(self.cards()), 1)

    def test_queued_card_is_dropped_once_the_item_leaves_the_human_queue(self):
        pipeline = self.build()
        with patch.object(dispatch_card.os, "replace", side_effect=OSError("dummy")),                 self.assertLogs("slack_dispatch.pipeline", level="ERROR"):
            result = pipeline.handle_event(event(text=f"<@{OWNER}> urgent"))
        self.store.set_route(result.message_id, "support", "human:console")
        pipeline.flush(1001.0)
        self.assertEqual(self.cards(), [])
        self.assertEqual(self.store._rows("SELECT 1 FROM human_notices WHERE adapter=2"), [])


class CardConfigTests(unittest.TestCase):
    def raw(self):
        return yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))

    def test_dispatch_card_entry_and_decisions_section(self):
        data = self.raw()
        data["notify_adapters"].append({"kind": "dispatch_card", "purpose": "human_queue",
                                        "dir": "dummy/state/dispatch_cards", "only_immediate": True,
                                        "workspace_domain": "example"})
        data["decisions"] = {"poll_seconds": 7, "hmac_key_file": "dummy/keys/dispatch.env"}
        config = parse_config(data)
        adapter = config.notify_adapters[-1]
        self.assertEqual((adapter.kind, adapter.path, adapter.purpose, adapter.only_immediate,
                          adapter.workspace_domain),
                         ("dispatch_card", "dummy/state/dispatch_cards", "human_queue", True, "example"))
        self.assertEqual((config.decisions.dir, config.decisions.poll_seconds,
                          config.decisions.hmac_key_file), (None, 7, "dummy/keys/dispatch.env"))

    def test_sample_defaults_and_invalid_values(self):
        config = parse_config(self.raw())
        self.assertEqual((config.decisions.dir, config.decisions.poll_seconds,
                          config.decisions.hmac_key_file), (None, 5, None))
        self.assertTrue(all(adapter.kind != "dispatch_card" for adapter in config.notify_adapters))
        bad = ({"kind": "dispatch_card", "dir": "dummy"},
               {"kind": "dispatch_card", "purpose": "human_queue"},
               {"kind": "dispatch_card", "purpose": "human_queue", "dir": "dummy",
                "workspace_domain": "https://bad"},
               {"kind": "dispatch_card", "purpose": "human_queue", "dir": "dummy",
                "only_immediate": "yes"})
        for entry in bad:
            with self.subTest(entry=entry):
                data = self.raw()
                data["notify_adapters"].append(entry)
                with self.assertRaises(ConfigError):
                    parse_config(data)
        for section in ({"poll_seconds": 0}, {"hmac_key_file": ""}, {"dir": 5}, []):
            with self.subTest(section=section):
                data = self.raw()
                data["decisions"] = section
                with self.assertRaises(ConfigError):
                    parse_config(data)


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
