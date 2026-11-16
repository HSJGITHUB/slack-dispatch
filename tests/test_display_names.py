"""P20 display names: one resolver (config, then ledger, then id), the CLI columns, the
directory name lookups for every author, `directory refresh` and the Deliveries empty state.

Dummy ids, dummy names and a dummy workspace domain only; no network.
"""
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from slack_dispatch.config.loader import ConfigError, load_config, parse_config
from slack_dispatch.config.schema import Console, UserDirectory as UserDirectoryConfig
from slack_dispatch.console import queries
from slack_dispatch.inbox import cli
from slack_dispatch.inbox.files import append, record
from slack_dispatch.ingest.user_directory import UserDirectory, parse_user
from slack_dispatch.ledger.store import DELIVERY_LOG_STARTED, META_SOURCE, LedgerStore
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import NAME_TRACK_LIMIT, Pipeline
from tests.test_pipeline_integration import SAMPLE, event, fixture_config
from tests.test_user_directory import FakeSlack

DOMAIN = "dummy-workspace"
PEOPLE_USER = "U0000000091"      # in the sample `people` map (Dummy Person One)
DIRECTORY_USER = "U0000000201"   # only in the ledger directory
UNKNOWN_USER = "U0000000299"     # nowhere: shown as the id
CONFIG_CHANNEL = "C0000000001"   # sample console.channels: dummy-support
PUBLIC = "C0000000211"
PRIVATE = "G0000000212"
DM = "D0000000213"
GROUP = "G0000000214"
UNKNOWN_CHANNEL = "C0000000299"
SEEN = "2026-11-13T00:00:00+00:00"


def membership_rows():
    return [dict(channel=PUBLIC, kind="public_channel", name="dummy-public", partner_user=None),
            dict(channel=PRIVATE, kind="private_channel", name="dummy-private", partner_user=None),
            dict(channel=DM, kind="im", name=None, partner_user=DIRECTORY_USER),
            dict(channel=GROUP, kind="mpim", name="mpdm-dummy", partner_user=None),
            # A config name wins over the membership name.
            dict(channel=CONFIG_CHANNEL, kind="public_channel", name="dummy-other", partner_user=None)]


class LedgerCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p20-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = fixture_config(self.root)
        self.store = LedgerStore(self.config.ledger.path, store_body=True)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id)

    def fill_names(self):
        self.store.put_user_directory(dict(user=DIRECTORY_USER, name="Dummy Directory Person"), SEEN)
        # A directory name never overrides the config `people` map.
        self.store.put_user_directory(dict(user=PEOPLE_USER, name="Dummy Directory Override"), SEEN)
        self.store.replace_membership(membership_rows(), SEEN)

    def with_domain(self, domain=DOMAIN):
        return replace(self.config, console=replace(self.config.console, workspace_domain=domain))

    def seed(self, ts, *, channel=PUBLIC, user=DIRECTORY_USER, thread=None, state="assigned",
             lane="support"):
        self.store.record_event(dict(source="slack", event_id="EvDUMMY" + channel + ts,
                                     event_type="message", channel=channel, user=user, ts=ts,
                                     thread_ts=thread, received_at=SEEN, text="DUMMY BODY"))
        mid = f"slack:{channel}:{ts}"
        if state == "human_pending":
            self.store.set_state(mid, "human_pending", note="none")
        else:
            self.store.set_route(mid, lane, "explicit_tag")
        return mid


class ResolverOrderTests(LedgerCase):
    def test_user_order_people_then_directory_then_id(self):
        self.fill_names()
        names = queries.DisplayNames(self.config, self.store)
        self.assertEqual(names.user(PEOPLE_USER), "Dummy Person One")
        self.assertEqual(names.user(DIRECTORY_USER), "Dummy Directory Person")
        self.assertEqual(names.user(UNKNOWN_USER), UNKNOWN_USER)
        self.assertIsNone(names.user_name(UNKNOWN_USER))
        self.assertIsNone(names.user_name(None))

    def test_channel_order_and_conversation_kinds(self):
        self.fill_names()
        names = queries.DisplayNames(self.config, self.store)
        self.assertEqual(names.channel(CONFIG_CHANNEL), "dummy-support")
        self.assertEqual(names.channel(PUBLIC), "#dummy-public")
        self.assertEqual(names.channel(PRIVATE), "#dummy-private")
        self.assertEqual(names.channel(DM), "DM:Dummy Directory Person")
        self.assertEqual(names.channel(GROUP), queries.GROUP_DM)
        self.assertEqual(names.channel(UNKNOWN_CHANNEL), UNKNOWN_CHANNEL)
        self.assertIsNone(names.channel_name(UNKNOWN_CHANNEL))

    def test_dm_partner_uses_the_user_order(self):
        self.store.replace_membership([dict(channel=DM, kind="im", partner_user=PEOPLE_USER),
                                       dict(channel="D0000000215", kind="im",
                                            partner_user=UNKNOWN_USER)], SEEN)
        names = queries.DisplayNames(self.config, self.store)
        self.assertEqual(names.channel(DM), "DM:Dummy Person One")
        self.assertEqual(names.channel("D0000000215"), "DM:" + UNKNOWN_USER)

    def test_one_read_per_table_per_instance(self):
        self.fill_names()
        with patch.object(self.store, "list_user_directory", wraps=self.store.list_user_directory) as users, \
                patch.object(self.store, "list_membership", wraps=self.store.list_membership) as rooms:
            names = queries.DisplayNames(self.config, self.store)
            for _ in range(3):
                names.user(DIRECTORY_USER)
                names.user(UNKNOWN_USER)
                names.channel(PUBLIC)
                names.channel(DM)
        self.assertEqual((users.call_count, rooms.call_count), (1, 1))

    def test_stores_without_tables_and_read_failures_show_ids(self):
        names = queries.DisplayNames(self.config, object())
        self.assertEqual((names.user(DIRECTORY_USER), names.channel(PUBLIC)), (DIRECTORY_USER, PUBLIC))
        broken = Mock(list_user_directory=Mock(side_effect=RuntimeError("dummy")),
                      list_membership=Mock(side_effect=RuntimeError("dummy")))
        with self.assertLogs("slack_dispatch.console.queries", "WARNING"):
            names = queries.DisplayNames(self.config, broken)
            self.assertEqual(names.user(DIRECTORY_USER), DIRECTORY_USER)
            self.assertEqual(names.channel(PUBLIC), PUBLIC)

    def test_unbound_view_keeps_config_names_and_bound_view_reads_the_ledger(self):
        self.fill_names()
        view = queries.View(self.config)
        self.assertEqual(view.person(DIRECTORY_USER), {"id": DIRECTORY_USER, "name": None})
        bound = view.bind(self.store)
        self.assertEqual(bound.person(DIRECTORY_USER), {"id": DIRECTORY_USER, "name": "Dummy Directory Person"})
        self.assertEqual(bound.channel(PUBLIC), {"id": PUBLIC, "name": "#dummy-public"})
        self.assertEqual(bound.channel(CONFIG_CHANNEL), {"id": CONFIG_CHANNEL, "name": "dummy-support"})
        self.assertIsNone(view.names)  # binding copies; the shared view is unchanged

    def test_queue_page_reads_each_table_once_and_carries_names(self):
        self.fill_names()
        for number, (channel, user) in enumerate([(PUBLIC, DIRECTORY_USER), (DM, UNKNOWN_USER),
                                                  (GROUP, PEOPLE_USER), (PUBLIC, DIRECTORY_USER)]):
            self.seed(f"1794528{number:03d}.000100", channel=channel, user=user, state="human_pending")
        config = self.with_domain()
        view = queries.View(config).bind(self.store)
        with patch.object(self.store, "list_user_directory", wraps=self.store.list_user_directory) as users, \
                patch.object(self.store, "list_membership", wraps=self.store.list_membership) as rooms:
            page = queries.queue_page(self.store, config, view, 1794529000.0)
        self.assertEqual((users.call_count, rooms.call_count), (1, 1))
        by_author = {item["author"]["id"]: item for item in page["items"]}
        self.assertEqual(by_author[DIRECTORY_USER]["author_name"], "Dummy Directory Person")
        self.assertEqual(by_author[DIRECTORY_USER]["channel_name"], "#dummy-public")
        self.assertEqual(by_author[UNKNOWN_USER]["author_name"], UNKNOWN_USER)
        self.assertEqual(by_author[UNKNOWN_USER]["channel_name"], "DM:Dummy Directory Person")
        self.assertEqual(by_author[PEOPLE_USER]["channel_name"], queries.GROUP_DM)
        self.assertEqual(by_author[PEOPLE_USER]["author"]["name"], "Dummy Person One")
        item = by_author[UNKNOWN_USER]
        self.assertEqual(item["permalink"], f"https://{DOMAIN}.slack.com/archives/{DM}/p1794528001000100")

    def test_scope_names_and_search_rows(self):
        self.fill_names()
        self.store.record_scope_evidence("channel", PUBLIC, "support", "assign")
        self.store.record_scope_evidence("person", DIRECTORY_USER, "support", "assign")
        view = queries.View(self.config).bind(self.store)
        rows = {row["key"]: row["name"] for row in queries.proposals(self.store, self.config, view)["proposals"]}
        self.assertEqual(rows, {PUBLIC: "#dummy-public", DIRECTORY_USER: "Dummy Directory Person"})
        mid = self.seed("1794528100.000100")
        found = queries.search(self.store, view, mid)["rows"][0]
        self.assertEqual((found["author_name"], found["channel_name"], found["permalink"]),
                         ("Dummy Directory Person", "#dummy-public", None))


class CliBase(LedgerCase):
    def setUp(self):
        super().setUp()
        self.fill_names()
        self.config_path = self.root / "config.yaml"
        self.write_config(DOMAIN)

    def write_config(self, domain):
        import yaml
        data = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        data["console"]["workspace_domain"] = domain
        data["ledger"]["path"] = self.config.ledger.path
        data["owner_resolver"] = {"enabled": False}
        self.config_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    def run_cli(self, *argv, config=True, client=None):
        out, err = io.StringIO(), io.StringIO()
        args = ["--inbox-root", str(self.root / "inbox"), *argv]
        if config:
            args += ["--config", str(self.config_path)]
        code = cli.main(args, store=self.store, stdout=out, stderr=err, client=client)
        return code, out.getvalue(), err.getvalue()

    def inbox_item(self, ts, **changes):
        mid = self.seed(ts, **changes)
        append(str(self.root / "inbox"), "support", [record(self.store.get_message(mid))])
        return mid


class CliTests(CliBase):
    def test_inbox_text_names_and_links_column(self):
        root = self.inbox_item("1794528000.000100")
        reply = self.inbox_item("1794528010.000100", thread="1794528000.000100", user=UNKNOWN_USER)
        code, out, _ = self.run_cli("inbox", "support")
        lines = out.splitlines()
        header = " ".join(cli.LIST_COLUMNS) + " channel_name author_name"
        self.assertEqual((code, lines[0]), (0, header))
        # fix1: existing columns keep ids (channel id, author without a people name); the
        # names are appended with spaces as `_`.
        columns = lines[1].split()
        # `when` is two tokens (MM-DD HH:MM).
        self.assertEqual((columns[4], columns[9], columns[10]), (PUBLIC, DIRECTORY_USER, "assigned"))
        self.assertTrue(lines[1].endswith(" #dummy-public Dummy_Directory_Person"))
        self.assertTrue(lines[2].startswith(reply))
        self.assertTrue(lines[2].endswith(" #dummy-public " + UNKNOWN_USER))
        code, out, _ = self.run_cli("inbox", "support", "--links")
        lines = out.splitlines()
        self.assertEqual(lines[0], header + " link")
        self.assertTrue(lines[1].endswith(f"https://{DOMAIN}.slack.com/archives/{PUBLIC}/p1794528000000100"))
        self.assertTrue(lines[2].endswith(f"https://{DOMAIN}.slack.com/archives/{PUBLIC}/p1794528010000100"
                                          f"?thread_ts=1794528000.000100&cid={PUBLIC}"))
        self.assertIn(root, lines[1])

    def test_inbox_json_keeps_ids_and_adds_names_and_permalinks(self):
        self.inbox_item("1794528000.000100")
        code, out, _ = self.run_cli("inbox", "support", "--json")
        row = json.loads(out)[0]
        self.assertEqual((code, row["channel"], row["channel_name"]), (0, PUBLIC, "#dummy-public"))
        self.assertEqual((row["author"], row["author_id"]), ("Dummy Directory Person", DIRECTORY_USER))
        self.assertEqual(row["permalink"], f"https://{DOMAIN}.slack.com/archives/{PUBLIC}/p1794528000000100")
        code, out, _ = self.run_cli("inbox", "support", "--thread", "--json")
        group = json.loads(out)[0]
        self.assertEqual((group["root"]["channel"], group["root"]["channel_name"]), (PUBLIC, "#dummy-public"))
        self.assertTrue(group["root"]["permalink"].endswith("/p1794528000000100"))

    def test_without_domain_links_are_dashes_and_json_has_no_permalink(self):
        self.write_config(None)
        self.inbox_item("1794528000.000100")
        code, out, _ = self.run_cli("inbox", "support", "--links")
        self.assertEqual(code, 0)
        self.assertTrue(out.splitlines()[1].endswith(" -"))
        row = json.loads(self.run_cli("inbox", "support", "--json")[1])[0]
        self.assertNotIn("permalink", row)
        code, out, _ = self.run_cli("inbox", "support", "--thread", "--links")
        self.assertIn(" link -", out.splitlines()[0])

    def test_inbox_without_config_still_uses_the_ledger_directory(self):
        self.inbox_item("1794528000.000100")
        code, out, _ = self.run_cli("inbox", "support", config=False)
        self.assertEqual(code, 0)
        self.assertTrue(out.splitlines()[1].endswith(" #dummy-public Dummy_Directory_Person"))

    def test_thread_text_root_link(self):
        self.inbox_item("1794528000.000100")
        self.inbox_item("1794528010.000100", thread="1794528000.000100")
        code, out, _ = self.run_cli("inbox", "support", "--thread", "--links", "--no-body")
        lines = out.splitlines()
        self.assertEqual(code, 0)
        self.assertIn(f"channel {PUBLIC} author {DIRECTORY_USER} ", lines[0])
        self.assertIn(" root channel_name #dummy-public author_name Dummy_Directory_Person link ", lines[0])
        self.assertIn(" Dummy_Directory_Person https://", lines[-1])
        self.assertTrue(lines[0].endswith(f"link https://{DOMAIN}.slack.com/archives/{PUBLIC}/p1794528000000100"))
        self.assertTrue(lines[-1].endswith("?thread_ts=1794528000.000100&cid=" + PUBLIC))

    def test_queue_owner_and_deliveries_use_names(self):
        mid = self.seed("1794528200.000100", channel=DM, state="human_pending")
        code, out, _ = self.run_cli("queue")
        self.assertEqual(code, 0)
        self.assertIn(f"{mid} none none DM:Dummy_Directory_Person Dummy_Directory_Person", out)
        code, out, _ = self.run_cli("owner", "--pending", "--json")
        row = json.loads(out)["rows"][0]
        self.assertEqual((row["channel"], row["channel_name"], row["author_name"]),
                         (DM, "DM:Dummy Directory Person", "Dummy Directory Person"))
        code, out, _ = self.run_cli("owner", PUBLIC)
        self.assertEqual(code, 0)
        self.assertIn(" name #dummy-public", out.splitlines()[0])
        self.store.replace_membership([dict(channel=DM, kind="im", partner_user=DIRECTORY_USER)], SEEN)
        code, out, _ = self.run_cli("owner", DM)
        self.assertTrue(out.splitlines()[0].endswith(" name DM:Dummy_Directory_Person"))
        self.store.add_delivery(dict(purpose="lane", lane="support", kind="new", status="sent",
                                     item_ids=[mid], created_at="2026-11-13T00:30:00.000000+00:00"))
        with patch.object(cli, "DELIVERY_CLOCK", lambda: datetime(2026, 11, 13, 1, tzinfo=timezone.utc).timestamp()):
            code, out, _ = self.run_cli("deliveries", "--json")
        item = json.loads(out)[0]["items"][0]
        self.assertEqual(item["channel"], {"id": DM, "name": "DM:Dummy Directory Person"})
        self.assertEqual(item["author"], {"id": DIRECTORY_USER, "name": "Dummy Directory Person"})
        self.assertTrue(item["permalink"].endswith(f"/archives/{DM}/p1794528200000100"))


class DirectoryRefreshTests(CliBase):
    def test_missing_only_looks_up_authors_without_a_row(self):
        for number, user in enumerate((DIRECTORY_USER, "U0000000202", "U0000000203")):
            self.seed(f"1794528{number:03d}.000100", user=user)
        client = FakeSlack({
            "U0000000202": {"ok": True, "user": {"id": "U0000000202", "name": "dummy.handle",
                                                 "real_name": "Dummy Real Name"}},
            "U0000000203": {"ok": True, "user": {"id": "U0000000203", "name": "dummy.only"}},
        })
        code, out, _ = self.run_cli("directory", "refresh", "--missing-only", client=client)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(call[2]["user"] for call in client.calls), ["U0000000202", "U0000000203"])
        self.assertTrue(all(call[0] == "users.info" for call in client.calls))
        self.assertEqual(out.strip(), "directory refresh mode=missing-only authors=2 looked_up=2 named=2 failed=0")
        self.assertEqual(self.store.get_user_directory("U0000000202")["name"], "Dummy Real Name")
        self.assertEqual(self.store.get_user_directory("U0000000203")["name"], "dummy.only")
        self.assertEqual(self.store.authors_without_directory(), [])
        # Nothing missing any more: a rerun makes no call.
        client.calls.clear()
        code, out, _ = self.run_cli("directory", "refresh", "--missing-only", "--json", client=client)
        self.assertEqual((code, json.loads(out)["authors"], client.calls), (0, 0, []))

    def test_full_refresh_and_failures(self):
        self.seed("1794528000.000100", user=DIRECTORY_USER)
        self.seed("1794528001.000100", user="U0000000204")
        client = FakeSlack({
            DIRECTORY_USER: {"ok": True, "user": {"id": DIRECTORY_USER, "real_name": "Dummy Renamed"}},
            "U0000000204": {"ok": False, "error": "user_not_found"},
        })
        with self.assertLogs("slack_dispatch.ingest.user_directory", "WARNING"):
            code, out, _ = self.run_cli("directory", "refresh", "--json", client=client)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), {"mode": "all", "authors": 2, "looked_up": 2, "named": 1,
                                           "failed": 1})
        self.assertEqual(self.store.get_user_directory(DIRECTORY_USER)["name"], "Dummy Renamed")

    def test_config_is_required_without_a_client(self):
        code, _, err = self.run_cli("directory", "refresh", config=False)
        self.assertEqual(code, 2)
        self.assertIn("Config is required", err)
        # A reconcile source whose token file does not exist: exit 2, class name only.
        import yaml
        data = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        for row in data["sources"]:
            row["env_file"] = str(self.root / "missing-dummy.env")
        self.config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
        code, _, err = self.run_cli("directory", "refresh")
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("Slack client could not be opened class="), err)
        # A config without a slack_reconcile source cannot build a client.
        data["sources"] = [row for row in data["sources"] if row["kind"] != "slack_reconcile"]
        self.config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
        code, _, err = self.run_cli("directory", "refresh")
        self.assertEqual(code, 2)
        self.assertIn("No slack_reconcile source configured", err)


class PipelineNameTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p20-pipeline-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = fixture_config(self.root)
        self.store = LedgerStore(self.config.ledger.path)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id, session_pointer="dummy-session", paused=False)
        self.now = 1000.0
        self.client = FakeSlack({
            "U0000000088": {"ok": True, "user": {"id": "U0000000088", "real_name": "Dummy Author"}},
            "U0000000089": {"ok": True, "user": {"id": "U0000000089", "name": "dummy-second"}},
        })

    def make(self, resolve_names=True):
        config = replace(self.config, user_directory=replace(self.config.user_directory,
                                                             resolve_names=resolve_names))
        clock = lambda: self.now  # noqa: E731
        pipeline = Pipeline(config, self.store, Mock(enabled=False, **{"suggest.return_value": None}),
                            [Mock(), Mock(), Mock()], WakePolicyEngine(config.wake_policy, clock), clock)
        pipeline.directory = UserDirectory(self.client, self.store, clock=clock)
        return pipeline

    def test_hand_posted_author_is_looked_up_from_flush(self):
        pipeline = self.make()
        pipeline.handle_event(event(1))                     # no bot_id: a hand-posted human
        self.assertEqual(self.client.calls, [])             # nothing on the event path
        pipeline.flush(self.now)
        self.assertEqual([call[2]["user"] for call in self.client.calls], ["U0000000088"])
        self.assertEqual(self.store.get_user_directory("U0000000088")["name"], "Dummy Author")
        pipeline.handle_event(event(2))                     # same author: checked, not queued
        pipeline.handle_event(event(3, user="U0000000089"))
        pipeline.handle_event(event(3, user="U0000000089"))  # duplicate event: ignored
        pipeline.flush(self.now)
        self.assertEqual([call[2]["user"] for call in self.client.calls], ["U0000000088", "U0000000089"])
        self.assertEqual(pipeline.snapshot()["directory_names"], 2)
        self.assertEqual(pipeline.snapshot()["directory_lookups"], 2)

    def test_resolve_names_off_keeps_p16_behaviour(self):
        pipeline = self.make(resolve_names=False)
        pipeline.handle_event(event(1))
        pipeline.flush(self.now)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(pipeline.snapshot()["directory_names"], 0)

    def test_fresh_rows_and_failures(self):
        self.store.put_user_directory(dict(user="U0000000088", name="Dummy Cached"),
                                      datetime.fromtimestamp(self.now, timezone.utc).isoformat())
        pipeline = self.make()
        pipeline.handle_event(event(1))
        pipeline.flush(self.now)
        self.assertEqual(self.client.calls, [])             # a fresh row needs no lookup
        self.client.answers["U0000000089"] = {"ok": False, "error": "user_not_found"}
        with self.assertLogs("slack_dispatch.ingest.user_directory", "WARNING"):
            pipeline.handle_event(event(2, user="U0000000089"))
            pipeline.flush(self.now)
        pipeline.handle_event(event(3, user="U0000000089"))
        pipeline.flush(self.now)                            # in the failure cooldown: no call
        self.assertEqual(len(self.client.calls), 1)
        self.assertIsNone(self.store.get_user_directory("U0000000089"))
        self.assertEqual(pipeline.snapshot()["directory_failures"], 1)

    def test_lookups_per_flush_are_capped(self):
        pipeline = self.make()
        for number in range(25):
            user = f"U0000000{300 + number}"
            self.client.answers[user] = {"ok": True, "user": {"id": user, "name": "dummy"}}
            pipeline.handle_event(event(number + 1, user=user))
        pipeline.flush(self.now)
        self.assertEqual(len(self.client.calls), 20)
        pipeline.flush(self.now)
        self.assertEqual(len(self.client.calls), 25)

    def test_without_directory_nothing_is_queued(self):
        pipeline = self.make()
        pipeline.directory = None
        pipeline.handle_event(event(1))
        pipeline.flush(self.now)
        self.assertEqual((self.client.calls, pipeline._name_queue), ([], {}))
        self.assertNotIn("directory_names", pipeline.snapshot())

    def test_lookup_runs_outside_the_pipeline_and_directory_locks(self):
        """fix1: while users.info is slow, handle_event (including an app post that needs the
        directory's own bot lookup) completes on another thread."""
        pipeline = self.make()
        slow_user, app_user = "U0000000088", "U0000000089"
        finished = []
        answer = self.client.answers[slow_user]

        def slow_answer():
            worker = threading.Thread(target=lambda: pipeline.handle_event(
                event(9, user=app_user, bot_id="B0000000077")))
            worker.start()
            worker.join(5)
            finished.append(not worker.is_alive())
            return answer

        original = self.client.api_call

        def api_call(method, http_verb="POST", params=None):
            if params["user"] == slow_user and not finished:
                self.client.calls.append((method, http_verb, dict(params)))
                return slow_answer()
            return original(method, http_verb, params)

        self.client.api_call = api_call
        pipeline.handle_event(event(1, user=slow_user))
        pipeline.flush(self.now)
        self.assertEqual(finished, [True])
        self.assertIsNotNone(self.store.get_message(f"slack:C0000000002:{event(9)['ts']}"))
        self.assertEqual(self.store.get_user_directory(slow_user)["name"], "Dummy Author")
        self.assertEqual(self.store.get_user_directory(app_user)["name"], "dummy-second")

    def test_time_budget_moves_the_rest_to_the_next_flush(self):
        pipeline = self.make()
        tick = [0.0]
        pipeline.name_clock = lambda: tick[0]
        users = [f"U0000000{400 + number}" for number in range(5)]
        original = self.client.api_call

        def api_call(method, http_verb="POST", params=None):
            tick[0] += 1.5  # each users.info call takes 1.5 seconds
            return original(method, http_verb, params)

        self.client.api_call = api_call
        for number, user in enumerate(users):
            self.client.answers[user] = {"ok": True, "user": {"id": user, "name": "dummy"}}
            pipeline.handle_event(event(number + 1, user=user))
        pipeline.flush(self.now)                 # 0 s, 1.5 s, then 3.0 s >= the 3 s budget
        self.assertEqual([call[2]["user"] for call in self.client.calls], users[:2])
        self.assertEqual(list(pipeline._name_queue), users[2:])
        pipeline.flush(self.now)
        pipeline.flush(self.now)
        self.assertEqual([call[2]["user"] for call in self.client.calls], users)
        self.assertEqual(pipeline._name_queue, {})

    def test_name_queue_and_checked_map_are_capped(self):
        pipeline = self.make()
        users = [f"U0000000{500 + number}" for number in range(5)]
        for user in users:
            self.client.answers[user] = {"ok": True, "user": {"id": user, "name": "dummy"}}
        with patch("slack_dispatch.pipeline.NAME_TRACK_LIMIT", 3):
            for number, user in enumerate(users):
                pipeline.handle_event(event(number + 1, user=user))
            self.assertEqual(list(pipeline._name_queue), users[2:])   # oldest dropped
            pipeline.flush(self.now)
            self.assertEqual(list(pipeline._names_checked), users[2:])
            pipeline.handle_event(event(10, user=users[0]))
            pipeline.handle_event(event(11, user=users[1]))
            pipeline.flush(self.now)
            self.assertEqual(list(pipeline._names_checked), [users[4], users[0], users[1]])
        self.assertEqual(NAME_TRACK_LIMIT, 5000)


class ParseAndConfigTests(unittest.TestCase):
    def test_parse_user_prefers_real_name(self):
        self.assertEqual(parse_user({"user": {"id": "U0000000201", "name": "dummy.handle",
                                              "real_name": "Dummy Real"}})["name"], "Dummy Real")
        self.assertEqual(parse_user({"user": {"id": "U0000000201", "name": "dummy.handle",
                                              "real_name": " "}})["name"], "dummy.handle")
        self.assertIsNone(parse_user({"user": {"id": "U0000000201"}})["name"])

    def test_config_keys(self):
        config = load_config(SAMPLE)
        self.assertIsNone(config.console.workspace_domain)
        self.assertTrue(config.user_directory.resolve_names)
        self.assertEqual((Console().workspace_domain, UserDirectoryConfig().resolve_names), (None, True))
        import yaml
        data = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        data["console"]["workspace_domain"] = DOMAIN
        data["user_directory"]["resolve_names"] = False
        parsed = parse_config(data)
        self.assertEqual((parsed.console.workspace_domain, parsed.user_directory.resolve_names),
                         (DOMAIN, False))
        for bad in ("Dummy", "dummy.slack.com", "-dummy", 7, ""):
            data["console"]["workspace_domain"] = bad
            with self.subTest(domain=bad), self.assertRaises(ConfigError):
                parse_config(data)
        data["console"]["workspace_domain"] = None
        data["user_directory"]["resolve_names"] = "yes"
        with self.assertRaises(ConfigError):
            parse_config(data)


class DeliveryEmptyStateTests(LedgerCase):
    NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc).timestamp()

    def test_log_start_is_stored_once_at_migration(self):
        stored = self.store.get_cursor(META_SOURCE, DELIVERY_LOG_STARTED)
        self.assertIsNotNone(stored)
        self.store.migrate()
        self.assertEqual(self.store.get_cursor(META_SOURCE, DELIVERY_LOG_STARTED), stored)
        self.assertEqual(self.store.delivery_log_started_at(), stored)
        # The meta row never shows up as a Slack channel.
        self.assertNotIn(DELIVERY_LOG_STARTED, self.store.list_channels())

    def test_earlier_first_row_wins_and_migration_uses_existing_rows(self):
        self.store.add_delivery(dict(purpose="lane", lane="support", status="sent", item_ids=[],
                                     created_at="2020-01-01T00:00:00.000000+00:00"))
        self.assertEqual(self.store.delivery_log_started_at(), "2020-01-01T00:00:00.000000+00:00")
        other = LedgerStore(self.root / "older.sqlite3")
        other.open()
        self.addCleanup(other.close)
        other._db.execute("CREATE TABLE delivery_log (id INTEGER PRIMARY KEY, created_at TEXT)")
        other._db.execute("INSERT INTO delivery_log (created_at) VALUES ('2026-11-01T00:00:00.000000+00:00')")
        other.migrate()
        self.assertEqual(other.get_cursor(META_SOURCE, DELIVERY_LOG_STARTED),
                         "2026-11-01T00:00:00.000000+00:00")

    def service_rows(self, *epochs):
        with self.store._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS service_deliveries (
                source TEXT NOT NULL, channel TEXT NOT NULL, thread_ts TEXT NOT NULL,
                lane TEXT NOT NULL, delivered_at REAL NOT NULL,
                PRIMARY KEY (source, channel, thread_ts, lane))""")
            for number, epoch in enumerate(epochs):
                db.execute("INSERT INTO service_deliveries VALUES ('slack', ?, ?, 'support', ?)",
                           (PUBLIC, f"{number}.0", epoch))

    def page(self, **filters):
        view = queries.View(self.config).bind(self.store)
        return queries.deliveries_page(self.store, self.config, view, self.NOW, **filters)

    def test_empty_window_reports_start_and_older_wakes(self):
        start = datetime(2026, 11, 16, 0, 0, tzinfo=timezone.utc)
        with self.store._transaction() as db:
            db.execute("UPDATE cursors SET cursor=? WHERE source=? AND channel=?",
                       (start.isoformat(timespec="microseconds"), META_SOURCE, DELIVERY_LOG_STARTED))
        hour = 3600
        self.service_rows(start.timestamp() - 2 * hour, start.timestamp() - 30 * hour,
                          start.timestamp() - 80 * hour, start.timestamp() + hour)
        page = self.page(hours=48)
        self.assertEqual((page["total"], page["rows"]), (0, []))
        self.assertEqual(page["log_started_at"], start.isoformat(timespec="microseconds"))
        self.assertEqual(page["log_started_when"], "2026-11-16 00:00:00")
        self.assertEqual(page["legacy_deliveries"], 2)          # inside 48h and before the start
        self.assertEqual(self.page(hours=0)["legacy_deliveries"], 3)
        self.assertEqual(self.page(hours=48, lane="content")["legacy_deliveries"], 0)
        self.assertEqual(self.page(hours=48, purpose="human_queue")["legacy_deliveries"], 0)
        self.assertEqual(self.page(hours=48, status="failed")["legacy_deliveries"], 0)
        self.assertEqual(self.page(hours=6)["legacy_deliveries"], 0)  # window after the start

    def test_non_empty_window_skips_the_legacy_count(self):
        self.service_rows(self.NOW - 3600)
        self.store.add_delivery(dict(purpose="lane", lane="support", status="sent", item_ids=[],
                                     created_at=datetime.fromtimestamp(self.NOW - 60, timezone.utc)
                                     .isoformat(timespec="microseconds")))
        page = self.page(hours=48)
        self.assertEqual((page["total"], page["legacy_deliveries"]), (1, 0))
        self.assertIsNotNone(page["log_started_at"])


if __name__ == "__main__":
    unittest.main()
