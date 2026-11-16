"""P17 owner resolver: registry reader, candidate tiers, pipeline hooks and CLI (dummy ids only)."""
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import time
import unittest
from unittest import mock

import yaml

from slack_dispatch.config.loader import ConfigError, parse_config
from slack_dispatch.config.schema import Lane, OwnerResolver as OwnerSettings
from slack_dispatch.inbox.cli import main as cli_main
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline
from slack_dispatch.resolver import (HarvestIndex, OwnerResolver, RegistryIndex, VERSION,
                                     lane_for_session, resolve)
from slack_dispatch.router.classifier import NullSuggester, Suggestion
from tests.fakes.fake_store import FakeStore
from tests.test_pipeline_integration import SAMPLE, event, fixture_config

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "registry_sample.json"
SUPPORT_SESSION = "0000aaaa-0000-4000-8000-000000000001"
CONTENT_SESSION = "2222cccc-0000-4000-8000-000000000004"
LANES = (Lane("owner", None, False), Lane("support", SUPPORT_SESSION, False),
         Lane("content", CONTENT_SESSION, True))
BASE = 1794528000


def ts(offset):
    return f"{BASE + offset}.000100"


def settings(path, **changes):
    values = dict(enabled=True, registry_path=str(path), harvest_path=None)
    values.update(changes)
    return OwnerSettings(**values)


def load_fixture():
    return RegistryIndex.parse(json.loads(FIXTURE.read_text(encoding="utf-8")), path=str(FIXTURE))


class RegistryParsingTests(unittest.TestCase):
    def setUp(self):
        self.index = load_fixture()

    def row(self, row_id):
        return next(row for row in self.index.rows if row.id == row_id)

    def test_field_aliases_and_normalization(self):
        self.assertEqual(len(self.index.rows), 12)  # the non-mapping entry is skipped
        open_row = self.row("DUMMY-ROOT-OPEN")
        self.assertEqual((open_row.channel, open_row.root, open_row.session_ids, open_row.closed),
                         ("C0000000002", ts(100), ("0000aaaa",), False))
        newer = self.row("DUMMY-CLOSED-NEWER")  # channel_id with a name, thread_ts alias
        self.assertEqual((newer.channel, newer.channel_raw, newer.root),
                         ("C0000000002", "C0000000002 #dummy-general", ts(200)))
        self.assertEqual((newer.closed, newer.last_ts), (True, ts(300)))  # date text ignored
        self.assertEqual(self.row("DUMMY-CLOSED-NOTS").channel, "C0000000002")  # channel alias
        self.assertEqual(self.row("DUMMY-CLOSED-NOTS").last_ts, None)
        self.assertTrue(self.row("DUMMY-CLOSED-OLDER").closed)  # status compared in lower case
        flat = self.row("DUMMY-FLAT-DM")
        self.assertEqual((flat.channel, flat.root), ("D0DUMMY0002", None))
        self.assertEqual(self.row("DUMMY-NAME-ONLY").channel, None)
        self.assertEqual(self.row("DUMMY-MULTI").channel, None)
        self.assertEqual(self.row("DUMMY-NO-CHANNEL").channel, None)
        self.assertEqual(self.row("DUMMY-DUP-A").session_ids, ("bbbb2222",))
        self.assertEqual(self.row("DUMMY-DUP-B").session_ids, ("0000aaaa",))
        self.assertEqual(self.row("DUMMY-DUP-C").session_ids, ())

    def test_duplicates_keep_all_rows_and_newest_wins(self):
        rows = self.index.rows_for("C0DUMMY0004", ts(500))
        self.assertEqual([row.id for row in rows], ["DUMMY-DUP-A", "DUMMY-DUP-B", "DUMMY-DUP-C"])
        self.assertEqual(self.index.match("C0DUMMY0004", ts(500)).id, "DUMMY-DUP-B")
        self.assertEqual(self.index.match("C0000000002", ts(250), ts(200)).id, "DUMMY-CLOSED-NEWER")
        self.assertIsNone(self.index.match("C0000000002", ts(999)))
        self.assertEqual([row.id for row in self.index.channel_rows("D0DUMMY0002")], ["DUMMY-FLAT-DM"])

    def test_meta_sweep_scope_and_routing_rules(self):
        sweep = {key: [(scope.kind, scope.session) for scope in value]
                 for key, value in self.index.sweep.items()}
        self.assertEqual(sweep, {"C0DUMMY0005": [("channel", "0000aaaa")],
                                 "C0DUMMY0007": [("channel", "0000aaaa")],
                                 "D0DUMMY0003": [("dm", "0000aaaa")],
                                 "C0DUMMY0008": [("channel", "2222cccc")]})
        self.assertEqual([(rule.channels, rule.sessions) for rule in self.index.rules],
                         [(("C0DUMMY0005",), ("0000aaaa", "bbbb2222")),
                          (("C0DUMMY0006",), ("2222cccc",)), ((), ())])
        self.assertIn("->", self.index.rules[1].text)

    def test_custom_closed_statuses_and_session_lane(self):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        index = RegistryIndex.parse(data, closed_statuses=("hold",))
        self.assertTrue(next(row for row in index.rows if row.id == "DUMMY-MULTI").closed)
        self.assertFalse(next(row for row in index.rows if row.id == "DUMMY-CLOSED-NEWER").closed)
        config = replace(parse_config(yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))), lanes=LANES)
        self.assertEqual((lane_for_session(config, "0000aaaa"), lane_for_session(config, "2222cccc"),
                          lane_for_session(config, "bbbb2222"), lane_for_session(config, None)),
                         ("support", "content", None, None))
        with self.assertRaises(ValueError):
            RegistryIndex.parse([])


class RegistryCacheTests(unittest.TestCase):
    def setUp(self):
        RegistryIndex.clear_cache()
        self.addCleanup(RegistryIndex.clear_cache)
        temp = tempfile.TemporaryDirectory(prefix="dispatch-registry-")
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "registry.json"
        shutil.copyfile(FIXTURE, self.path)

    def bump(self, text):
        self.path.write_text(text, encoding="utf-8")
        stamp = self.path.stat().st_mtime + 5
        os.utime(self.path, (stamp, stamp))

    def test_mtime_cache_reload_and_failure_keeps_previous(self):
        first = RegistryIndex.load(self.path)
        self.assertIs(RegistryIndex.load(self.path), first)
        self.bump(json.dumps({"threads": [{"id": "DUMMY-NEW", "ch": "C0DUMMY0001",
                                           "root_ts": ts(1), "status": "open"}]}))
        second = RegistryIndex.load(self.path)
        self.assertIsNot(second, first)
        self.assertEqual([row.id for row in second.rows], ["DUMMY-NEW"])
        self.bump("{not json")
        with self.assertLogs("slack_dispatch.resolver.registry", level="WARNING") as logs:
            kept = RegistryIndex.load(self.path)
        self.assertIs(kept, second)
        self.assertEqual(logs.output, ["WARNING:slack_dispatch.resolver.registry:"
                                       "Registry load failed class=JSONDecodeError"])
        with self.assertNoLogs("slack_dispatch.resolver.registry", level="WARNING"):
            self.assertIs(RegistryIndex.load(self.path), second)

    def test_missing_file_is_empty_and_warns_once(self):
        missing = self.path.with_name("missing.json")
        with self.assertLogs("slack_dispatch.resolver.registry", level="WARNING") as logs:
            index = RegistryIndex.load(missing)
        self.assertEqual((index.rows, len(logs.output)), ([], 1))
        self.assertIn("class=FileNotFoundError", logs.output[0])
        with self.assertNoLogs("slack_dispatch.resolver.registry", level="WARNING"):
            RegistryIndex.load(missing)

    def test_owner_resolver_checks_the_file_at_most_every_refresh_seconds(self):
        config = replace(fixture_config(self.path.parent), lanes=LANES,
                         owner_resolver=settings(self.path, refresh_seconds=300))
        now = [0.0]
        owner = OwnerResolver(config, clock=lambda: now[0])
        first = owner.registry()
        self.bump(json.dumps({"threads": []}))
        self.assertIs(owner.registry(), first)  # within refresh_seconds: not checked
        now[0] = 301.0
        self.assertEqual(owner.registry().rows, [])


class ResolverTiersTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-resolver-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        base = fixture_config(self.root)
        self.config = replace(base, lanes=LANES, owner_resolver=settings(FIXTURE),
                              channel_defaults=dict(base.channel_defaults, C0DUMMY0005="owner"))
        self.store = LedgerStore(self.config.ledger.path)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in ("owner", "support", "content"):
            self.store.upsert_lane(lane)
        self.registry = load_fixture()

    def tiers(self, resolution):
        return [(c.tier, c.lane, c.source) for c in resolution.candidates]

    def test_each_tier_in_order(self):
        self.store.bind_thread("slack", "C0DUMMY0005", ts(1), "content")
        self.store.confirm_scope_claim("support", "thread", "C0DUMMY0005:" + ts(1), by="dummy")
        self.store.confirm_scope_claim("content", "channel", "C0DUMMY0005", by="dummy")
        result = resolve(self.store, self.config, self.registry, "C0DUMMY0005", ts(1))
        self.assertEqual(self.tiers(result), [
            ("certain", "content", "binding"), ("certain", "support", "scope_claim"),
            ("certain", "content", "scope_claim"), ("certain", "owner", "channel_default"),
            ("declared", "support", "sweep_scope"), ("declared", "support", "routing_rule"),
            ("declared", None, "routing_rule")])
        self.assertEqual((result.best.lane, result.confidence), ("content", "certain"))
        self.assertEqual(result.as_dict()["candidates"][-1]["session"], "bbbb2222")

    def test_registry_rows_declared_and_recorded(self):
        result = resolve(self.store, self.config, self.registry, "C0DUMMY0004", ts(500))
        self.assertEqual(self.tiers(result), [("declared", "support", "registry"),
                                              ("declared", None, "registry"),
                                              ("recorded", None, "registry")])
        self.assertEqual(result.best.lane, "support")
        self.assertEqual(result.registry_row["id"], "DUMMY-DUP-B")
        self.assertIn("owner Dummy Person", result.candidates[2].evidence)
        recorded = resolve(self.store, self.config, self.registry, "C0000000002", ts(400))
        self.assertEqual((self.tiers(recorded), recorded.best), ([("recorded", None, "registry")], None))
        flat = resolve(self.store, self.config, self.registry, "D0DUMMY0002", ts(1))
        self.assertEqual(self.tiers(flat), [("recorded", None, "registry_channel")])
        unknown = resolve(self.store, self.config, self.registry, "C0DUMMY0009", ts(900))
        self.assertEqual([(c.lane, c.session) for c in unknown.candidates], [(None, "bbbb2222")])

    def test_observed_activity_and_harvest(self):
        for offset, lane, state in ((1, "support", "done"), (2, "support", "acked"), (3, "content", "done"),
                                    (4, "owner", "assigned")):
            mid = f"slack:C0DUMMY0010:{ts(offset)}"
            self.store.record_event(dict(source="slack", event_id="EvDUMMY" + str(offset),
                                         event_type="message", channel="C0DUMMY0010", user="U0000000088",
                                         ts=ts(offset), received_at="2026-11-13T00:00:00+00:00",
                                         text="DUMMY BODY TEXT"))
            self.store.set_route(mid, lane, "test", state=state)
        harvest = HarvestIndex(HarvestIndex.parse_lines([
            json.dumps({"lane": "content", "tier": "strong", "channels": {"C0DUMMY0010": 3}}),
            json.dumps({"lane": "content", "tier": "weak(snapshot)", "channels": {"C0DUMMY0010": 1}}),
            json.dumps({"lane": "owner", "tier": "weak(snapshot)", "dms": {}, "thread_ts": {ts(9): 1}}),
            json.dumps({"lane": "NONLANE", "tier": "strong", "channels": {"C0DUMMY0010": 1}}),
            "not json", ""]))
        result = resolve(self.store, self.config, None, "C0DUMMY0010", ts(9), harvest=harvest)
        self.assertEqual(self.tiers(result), [("observed", "support", "activity"),
                                              ("observed", "content", "activity"),
                                              ("observed", "content", "harvest"),
                                              ("observed", "owner", "harvest")])
        self.assertEqual(result.candidates[2].evidence,
                         "harvest files naming C0DUMMY0010: strong 1, weak 1")
        self.assertNotIn("DUMMY BODY TEXT", json.dumps(result.as_dict()))
        later = resolve(self.store, self.config, None, "C0DUMMY0010", harvest=HarvestIndex(),
                        now=time.time() + 61 * 86400)
        self.assertEqual(later.candidates, [])

    def test_harvest_file_is_optional(self):
        self.assertEqual(HarvestIndex.load(None).rows, [])
        self.assertEqual(HarvestIndex.load(self.root / "missing.jsonl").rows, [])
        path = self.root / "harvest.jsonl"
        path.write_text(json.dumps({"lane": "support", "tier": "strong", "channels": {"C0DUMMY0011": 2}})
                        + "\n", encoding="utf-8")
        config = replace(self.config, owner_resolver=settings(FIXTURE, harvest_path=str(path)))
        result = resolve(self.store, config, None, "C0DUMMY0011")
        self.assertEqual(self.tiers(result), [("observed", "support", "harvest")])

    def test_evidence_and_reason_are_trimmed(self):
        data = {"threads": [{"id": "X" * 300, "ch": "C0DUMMY0012", "root_ts": ts(1), "status": "open",
                             "owner": "session 0000aaaa " + "y" * 400}]}
        result = resolve(self.store, self.config, RegistryIndex.parse(data), "C0DUMMY0012", ts(1))
        self.assertLessEqual(len(result.best.evidence), 160)
        self.assertLessEqual(len(result.reason()), 200)
        self.assertTrue(result.reason().startswith("registry: registry "))
        self.assertNotIn("\n", result.reason())

    def test_resolver_never_calls_the_network(self):
        store = FakeStore()
        store.bind_thread("slack", "C0DUMMY0005", ts(1), "content")

        def refuse(*args, **kwargs):
            raise AssertionError("network access attempted")

        with mock.patch.object(socket, "socket", side_effect=refuse), \
                mock.patch.object(socket, "create_connection", side_effect=refuse):
            result = resolve(store, self.config, self.registry, "C0DUMMY0005", ts(1))
        self.assertEqual(result.best.source, "binding")
        self.assertIn(("declared", "support", "sweep_scope"), self.tiers(result))


class OwnerConfigTests(unittest.TestCase):
    def data(self):
        return yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))

    def test_defaults_and_section(self):
        data = self.data()
        data.pop("owner_resolver", None)
        self.assertEqual(parse_config(data).owner_resolver, OwnerSettings())
        self.assertFalse(OwnerSettings().enabled)
        data["owner_resolver"] = {"enabled": True, "registry_path": "dummy/registry.json",
                                  "harvest_path": None, "refresh_seconds": 60,
                                  "closed_statuses": ["Closed", "done"],
                                  "close_queue_on_owner_reply": False, "activity_days": 30}
        section = parse_config(data).owner_resolver
        self.assertEqual((section.enabled, section.registry_path, section.refresh_seconds,
                          section.closed_statuses, section.close_queue_on_owner_reply,
                          section.activity_days),
                         (True, "dummy/registry.json", 60, ("closed", "done"), False, 30))
        self.assertFalse(parse_config(self.data()).owner_resolver.enabled)  # sample keeps it off

    def test_invalid_values(self):
        for bad in ({"enabled": "yes"}, {"refresh_seconds": 0}, {"closed_statuses": []},
                    {"closed_statuses": [""]}, {"registry_path": ""}, {"activity_days": -1},
                    {"close_queue_on_owner_reply": 1}, []):
            data = self.data()
            data["owner_resolver"] = bad
            with self.assertRaises(ConfigError):
                parse_config(data)


class RecordingSuggester:
    enabled = True

    def __init__(self, lane="content"):
        self.lane, self.calls = lane, []

    def suggest(self, message):
        self.calls.append(message["id"])
        return Suggestion(self.lane, "dummy classifier reason", "v1+p1")


class PipelineHookTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-owner-pipeline-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        base = fixture_config(self.root)
        self.config = replace(base, lanes=LANES, owner_resolver=settings(FIXTURE),
                              human_queue=replace(base.human_queue, mode="digest"))
        self.store = LedgerStore(self.config.ledger.path)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id)
        self.now = 1000.0
        self.notifiers = [FileInboxNotifier(self.config.inbox.root, self.store.get_message),
                          mock.Mock(), mock.Mock()]
        self.suggester = RecordingSuggester()
        self.pipeline = self.make()

    def make(self, config=None):
        config = config or self.config
        return Pipeline(config, self.store, self.suggester, self.notifiers,
                        WakePolicyEngine(config.wake_policy, lambda: self.now), lambda: self.now)

    def handle(self, number, **changes):
        changes.setdefault("text", "plain dummy text")
        return self.pipeline.handle_event(event(number, **changes))

    def test_suggestion_for_human_pending_only_and_classifier_fills_the_rest(self):
        resolved = self.handle(1, ts=ts(100))
        row = self.store.get_message(resolved.message_id)
        self.assertEqual((row["state"], row["classifier_suggestion"], row["classifier_version"]),
                         ("human_pending", "support", VERSION))
        self.assertTrue(row["suggestion_reason"].startswith("registry: registry DUMMY-ROOT-OPEN"))
        self.assertEqual(self.suggester.calls, [])  # the resolver answered first
        unresolved = self.handle(2, channel="C0DUMMY0013", ts=ts(2))
        row = self.store.get_message(unresolved.message_id)
        self.assertEqual((row["classifier_suggestion"], row["classifier_version"]), ("content", "v1+p1"))
        self.assertEqual(self.suggester.calls, [unresolved.message_id])
        assigned = self.handle(3, ts=ts(3), text="[support] tagged")
        row = self.store.get_message(assigned.message_id)
        self.assertEqual((row["state"], row["classifier_suggestion"]), ("assigned", None))
        protected = self.handle(4, ts="1794528100.000200",
                                thread_ts=ts(100), user="U0000000091", is_thread_reply=True)
        row = self.store.get_message(protected.message_id)
        self.assertEqual((row["state"], row["attention"], row["classifier_suggestion"]),
                         ("human_pending", "always_human", "support"))
        self.assertEqual(len(self.suggester.calls), 1)  # always_human never asks the classifier

    def test_disabled_resolver_leaves_the_classifier_path_unchanged(self):
        self.config = replace(self.config, owner_resolver=replace(self.config.owner_resolver, enabled=False))
        self.pipeline = self.make()
        self.assertIsNone(self.pipeline.owner_resolver)
        result = self.handle(1, ts=ts(100))
        row = self.store.get_message(result.message_id)
        self.assertEqual((row["classifier_suggestion"], self.suggester.calls),
                         ("content", [result.message_id]))
        self.assertNotIn("owner_reply_closed", self.pipeline.snapshot())

    def test_null_classifier_and_resolver_failure(self):
        self.suggester = NullSuggester()
        self.pipeline = self.make()
        with mock.patch("slack_dispatch.pipeline.suggestion_for", side_effect=RuntimeError("x")), \
                self.assertLogs("slack_dispatch.pipeline", level="WARNING") as logs:
            result = self.handle(1, ts=ts(100))
        self.assertEqual(self.store.get_message(result.message_id)["state"], "human_pending")
        self.assertIsNone(self.store.get_message(result.message_id)["classifier_suggestion"])
        self.assertIn("Owner resolver skipped class=RuntimeError", logs.output[0])

    def owner_reply(self, number, reply_ts, root):
        return self.handle(number, ts=reply_ts, thread_ts=root, user="U0000000093", is_thread_reply=True)

    def notices(self, mid):
        return self.store._rows("SELECT * FROM human_notices WHERE message_id=?", (mid,))

    def test_owner_reply_closes_only_earlier_pending_items_of_the_thread(self):
        root = ts(10)
        first = self.handle(1, ts=root).message_id
        reply = self.handle(2, ts="1794528010.000200", thread_ts=root, is_thread_reply=True).message_id
        lane_item = self.handle(3, ts="1794528010.000300", thread_ts=root, is_thread_reply=True,
                                text="[content] tagged").message_id
        later = self.handle(4, ts="1794528010.000900", thread_ts=root, is_thread_reply=True).message_id
        self.store.set_state(later, "human_pending", note="none")  # a later item stays queued
        other = self.handle(5, ts=ts(20)).message_id
        self.assertTrue(self.notices(first))
        self.owner_reply(6, "1794528010.000500", root)
        states = {mid: (self.store.get_message(mid)["state"], self.store.get_message(mid)["note"])
                  for mid in (first, reply, lane_item, later, other)}
        self.assertEqual(states, {first: ("done", "owner_reply"), reply: ("done", "owner_reply"),
                                  lane_item: ("assigned", None),
                                  later: ("human_pending", "none"), other: ("human_pending", "none")})
        self.assertEqual((self.notices(first), self.notices(reply)), ([], []))
        self.assertTrue(self.notices(other))
        self.assertEqual(self.pipeline.snapshot()["owner_reply_closed"], 2)
        exported = (self.root / "state/human_queue.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(first, exported)

    def test_owner_root_post_and_switch_off_close_nothing(self):
        first = self.handle(1, ts=ts(10)).message_id
        self.handle(2, ts=ts(11), user="U0000000093")  # a new root by the owner, not a reply
        self.assertEqual(self.store.get_message(first)["state"], "human_pending")
        self.config = replace(self.config, owner_resolver=replace(self.config.owner_resolver,
                                                                  close_queue_on_owner_reply=False))
        self.pipeline = self.make()
        self.owner_reply(3, "1794528010.000500", ts(10))
        self.assertEqual(self.store.get_message(first)["state"], "human_pending")
        self.config = replace(self.config, owner_resolver=OwnerSettings())
        self.pipeline = self.make()
        self.owner_reply(4, "1794528010.000600", ts(10))
        self.assertEqual(self.store.get_message(first)["state"], "human_pending")


class OwnerCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-owner-cli-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        data = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        data["lanes"] = [{"id": "owner", "session": None, "paused": False},
                         {"id": "support", "session": SUPPORT_SESSION, "paused": False},
                         {"id": "content", "session": CONTENT_SESSION, "paused": True}]
        data["owner_resolver"] = {"enabled": False, "registry_path": str(FIXTURE)}
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
        self.store = LedgerStore(self.root / "ledger.sqlite3", store_body=True)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in ("owner", "support", "content"):
            self.store.upsert_lane(lane)

    def pending(self, channel, message_ts, thread=None):
        self.store.record_event(dict(source="slack", event_id="EvDUMMY" + channel + message_ts,
                                     event_type="message", channel=channel, user="U0000000088",
                                     ts=message_ts, thread_ts=thread, received_at="2026-11-13T00:00:00+00:00",
                                     text="DUMMY BODY"))
        mid = f"slack:{channel}:{message_ts}"
        self.store.set_state(mid, "human_pending", note="none")
        return mid

    def run_cli(self, *argv, config=True):
        out, err = io.StringIO(), io.StringIO()
        args = list(argv) + (["--config", str(self.config_path)] if config else [])
        code = cli_main(args, store=self.store, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_owner_pending_table_and_apply(self):
        resolved = self.pending("C0000000002", ts(100))
        nothing = self.pending("C0DUMMY0013", ts(1))
        code, out, _ = self.run_cli("owner", "--pending")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "id channel thread best_lane confidence source")
        self.assertIn(f"{resolved} C0000000002 - support declared registry", lines)
        self.assertIn(f"{nothing} C0DUMMY0013 - - - -", lines)
        self.assertIn("pending=2 with_lane=1 applied=0", lines)
        self.assertIsNone(self.store.get_message(resolved)["classifier_suggestion"])  # dry run
        code, out, _ = self.run_cli("owner", "--pending", "--apply", "--json")
        data = json.loads(out)
        self.assertEqual((code, data["mode"], data["applied"]), (0, "apply", 1))
        row = self.store.get_message(resolved)
        self.assertEqual((row["classifier_suggestion"], row["classifier_version"]), ("support", VERSION))
        self.assertIsNone(self.store.get_message(nothing)["classifier_suggestion"])

    def test_owner_single_lookup(self):
        reply = self.pending("C0000000002", "1794528100.000500", thread=ts(100))
        code, out, _ = self.run_cli("owner", "C0000000002", "--ts", "1794528100.000500", "--json")
        data = json.loads(out)
        self.assertEqual((code, data["thread_ts"], data["root"], data["best"]["lane"]),
                         (0, ts(100), ts(100), "support"))
        self.assertEqual(set(data["candidates"][0]), {"lane", "session", "confidence", "source", "evidence"})
        self.assertEqual(data["registry_row"]["id"], "DUMMY-ROOT-OPEN")
        code, out, _ = self.run_cli("owner", "C0DUMMY0005")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("channel C0DUMMY0005 root - best support (declared, sweep_scope)"))
        self.assertNotIn("DUMMY BODY", out)
        self.assertIsNotNone(reply)

    def test_owner_input_errors(self):
        self.assertEqual(self.run_cli("owner", "--pending", config=False)[0], 2)
        self.assertEqual(self.run_cli("owner")[0], 2)
        self.assertEqual(self.run_cli("owner", "C0DUMMY0005", "--pending")[0], 2)
        self.assertEqual(self.run_cli("owner", "C0DUMMY0005", "--apply")[0], 2)
        self.assertEqual(self.run_cli("owner", "not-a-channel")[0], 2)
        self.assertEqual(self.run_cli("owner", "C0DUMMY0005", "--ts", "x")[0], 2)


if __name__ == "__main__":
    unittest.main()
