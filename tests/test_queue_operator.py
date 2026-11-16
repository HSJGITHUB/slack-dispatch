"""P25 operator queue: human_pending plus the direct lane in one queue (dummy ids only)."""
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from slack_dispatch.config.schema import HumanQueue
from slack_dispatch.console import actions, queries
from slack_dispatch.inbox import cli
from slack_dispatch.notify.file_drop import FileDropNotifier
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline
from slack_dispatch.queue.human import digest_counts_text, operator_queue, queue_counts
from slack_dispatch.router.classifier import NullSuggester
from tests.fakes.fake_store import FakeStore
from tests.test_console_app import DEAD_ID, LIVE_ID, ConsoleCase, stamp, ts
from tests.test_console_core import open_store, seed
from tests.test_pipeline_integration import event, fixture_config

STATIC = Path(queries.__file__).with_name("static")


class OperatorQueueCase(ConsoleCase):
    def direct(self, offset, *, text=None, state="assigned"):
        """An item the router put in the direct lane `owner` (state assigned or acked)."""
        mid = seed(self.store, ts(offset), text=text, received=stamp(offset))
        self.store.set_route(mid, "owner", "rule:dummy")
        if state != "assigned":
            self.store.set_state(mid, state)
        return mid

    def commit(self, decisions):
        return self.post("/api/queue/commit", {"decisions": decisions})


class QueuePageTests(OperatorQueueCase):
    def test_both_kinds_newest_first_with_kind_and_counts(self):
        old_direct = self.direct(1, text="DUMMY_D1")
        pending = self.pending(2, text="DUMMY_P2", suggestion="support")
        acked = self.direct(3, state="acked")
        newest = self.pending(4)
        other = seed(self.store, ts(5), received=stamp(5))
        self.store.set_route(other, "support", "rule:dummy")  # another lane: not in the queue
        done = self.direct(6)
        self.store.set_state(done, "done")  # closed direct item: not in the queue
        context = self.direct(7)
        self.store.set_state(context, "assigned", note="context")  # never an inbox item
        data = self.get("/api/queue")
        self.assertEqual([(row["id"], row["queue_kind"]) for row in data["items"]],
                         [(newest, "undecided"), (acked, "direct"), (pending, "undecided"),
                          (old_direct, "direct")])
        self.assertEqual((data["total"], data["counts"], data["kind"], data["direct_lane"]),
                         (4, {"undecided": 2, "direct": 2}, "all", "owner"))
        self.assertEqual(data["pending_ids"], [newest, acked, pending, old_direct])
        rows = {row["id"]: row for row in data["items"]}
        self.assertEqual((rows[old_direct]["lane"], rows[old_direct]["state"], rows[old_direct]["body"]),
                         ("owner", "assigned", "DUMMY_D1"))
        self.assertEqual(rows[acked]["state"], "acked")
        self.assertEqual((rows[pending]["lane"], rows[pending]["suggestion"]["lane"]), (None, "support"))
        self.assertFalse(any(row["direct_session"] for row in data["items"]))
        # The same row shape as P10 to P21 for both kinds: names, permalinks, hints, DM flag.
        for key in ("author_name", "channel_name", "permalink", "hints", "is_dm", "registry", "root"):
            self.assertIn(key, rows[old_direct])

    def test_filter_parameter(self):
        direct = self.direct(1)
        pending = self.pending(2)
        only_direct = self.get("/api/queue?kind=direct")
        self.assertEqual([row["id"] for row in only_direct["items"]], [direct])
        self.assertEqual((only_direct["total"], only_direct["counts"]), (1, {"undecided": 1, "direct": 1}))
        # pending_ids always cover both kinds, so staged rows hidden by the filter survive.
        self.assertEqual(only_direct["pending_ids"], [pending, direct])
        only_pending = self.get("/api/queue?kind=undecided")
        self.assertEqual([row["id"] for row in only_pending["items"]], [pending])
        self.get("/api/queue?kind=other", 422)
        with self.assertRaises(ValueError):
            queries.queue_page(self.store, self.config, queries.View(self.config), 0, kind="other")

    def test_counts_endpoint_and_operator_name(self):
        self.direct(1)
        self.pending(2)
        self.pending(3)
        self.assertEqual(self.get("/api/queue/counts"), {"undecided": 2, "direct": 1, "total": 3})
        self.assertEqual(self.get("/api/config")["operator_name"], "Dummy Owner")
        unnamed = replace(self.config, people={})
        self.assertIsNone(self.get("/api/config", client=self.make_client(unnamed))["operator_name"])

    def test_without_a_configured_direct_lane_the_queue_is_human_pending_only(self):
        self.direct(1)
        pending = self.pending(2)
        config = replace(self.config, console=replace(self.config.console, direct_lane="owner-direct"))
        data = self.get("/api/queue", client=self.make_client(config))
        self.assertEqual(([row["id"] for row in data["items"]], data["counts"], data["direct_lane"]),
                         ([pending], {"undecided": 1, "direct": 0}, None))


class CommitTests(OperatorQueueCase):
    def test_direct_rows_cannot_be_staged_as_direct_again(self):
        mid = self.direct(1)
        before = dict(self.store.get_message(mid))
        data = self.commit([{"id": mid, "action": "direct"}])
        self.assertEqual([(row["status"], row["detail"]) for row in data["results"]],
                         [("error", "already in the direct lane")])
        to_itself = self.commit([{"id": mid, "action": "assign", "lane": "owner"}])["results"][0]
        self.assertEqual((to_itself["status"], to_itself["detail"]), ("error", "already in the direct lane"))
        now = self.post("/api/queue/actions", {"action": "direct", "ids": [mid]})["results"][0]
        self.assertEqual((now["ok"], now["error"], now["detail"]), (False, "invalid", "already in the direct lane"))
        self.assertEqual(dict(self.store.get_message(mid)), before)
        self.assertIsNone(data["undo"])

    def test_assign_of_a_direct_row_reroutes_journals_and_undoes(self):
        mid = self.direct(1)
        data = self.commit([{"id": mid, "action": "assign", "lane": "support"}])
        row = data["results"][0]
        self.assertEqual((row["status"], row["lane"]), ("applied", "support"))
        message = self.store.get_message(mid)
        self.assertEqual((message["lane"], message["state"], message["route_reason"]),
                         ("support", "assigned", "reroute:owner"))
        records = [json.loads(line) for line in
                   (self.inbox / "support.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(record["id"], record["kind"]) for record in records], [(mid, "reroute")])
        self.assertIn(mid, self.store.pending_wake_ids("support"))
        self.assertEqual((data["undo"]["action"], data["undo"]["count"]), ("commit", 1))
        self.assertEqual(self.get("/api/queue")["counts"], {"undecided": 0, "direct": 0})
        undone = self.post(f"/api/undo/{data['undo']['action_id']}", {})
        self.assertEqual(undone["counts"]["restored"], 1)
        restored = self.store.get_message(mid)
        self.assertEqual((restored["lane"], restored["state"], restored["route_reason"]),
                         ("owner", "assigned", "rule:dummy"))
        self.assertEqual([row["id"] for row in self.get("/api/queue?kind=direct")["items"]], [mid])

    def test_done_and_exclude_on_direct_rows_and_stale_rows(self):
        first, second, third = self.direct(1), self.direct(2, state="acked"), self.direct(3)
        self.store.set_route(third, "content", "reroute:owner")  # moved meanwhile
        data = self.commit([{"id": first, "action": "done", "note": " dummy done "},
                            {"id": second, "action": "exclude"},
                            {"id": third, "action": "done"}])
        self.assertEqual([(row["status"], row["detail"]) for row in data["results"]],
                         [("applied", None), ("applied", None), ("conflict", "state is assigned")])
        self.assertEqual((self.store.get_message(first)["state"], self.store.get_message(first)["note"]),
                         ("done", "dummy done"))
        self.assertEqual((self.store.get_message(second)["state"], self.store.get_message(second)["note"]),
                         ("excluded", "human:console"))
        self.assertEqual(self.store.get_message(third)["lane"], "content")
        self.assertEqual(self.get("/api/queue")["total"], 0)

    def test_mixed_go_handles_both_kinds(self):
        pending, direct = self.pending(1), self.direct(2)
        data = self.commit([{"id": pending, "action": "direct"},
                            {"id": direct, "action": "assign", "lane": "content"}])
        self.assertEqual([row["status"] for row in data["results"]], ["applied", "applied"])
        self.assertEqual((self.store.get_message(pending)["lane"], self.store.get_message(direct)["lane"]),
                         ("owner", "content"))
        queue = self.get("/api/queue")
        self.assertEqual([(row["id"], row["queue_kind"]) for row in queue["items"]], [(pending, "direct")])

    def test_operator_guard(self):
        guard = actions.operator_guard("owner")
        for row in ({"state": "human_pending"}, {"state": "assigned", "lane": "owner"},
                    {"state": "acked", "lane": "owner"}):
            guard(row)
        for row, detail in (({"state": "done", "lane": "owner"}, "state is done"),
                            ({"state": "assigned", "lane": "support"}, "state is assigned")):
            with self.assertRaises(actions.ActionError) as caught:
                guard(row)
            self.assertEqual((caught.exception.code, caught.exception.detail), ("conflict", detail))
        with self.assertRaises(actions.ActionError):
            actions.operator_guard(None)({"state": "assigned", "lane": "owner"})


class LanesTabTests(OperatorQueueCase):
    def test_direct_lane_card_hidden_without_session_and_shown_with_one(self):
        self.direct(1)
        data = self.get("/api/lanes")
        cards = {card["lane"]: card for card in data["lanes"]}
        self.assertEqual(data["direct_lane"],
                         {"lane": "owner", "session_attached": False, "listed": False, "queue_count": 1,
                          "session_down": False})
        self.assertEqual((cards["owner"]["direct"], cards["owner"]["listed"]), (True, False))
        self.assertEqual((cards["support"]["direct"], cards["support"]["listed"]), (False, True))
        self.store.upsert_lane("owner", session_pointer="dummy-assistant")  # a session claims it
        data = self.get("/api/lanes")
        cards = {card["lane"]: card for card in data["lanes"]}
        self.assertEqual((data["direct_lane"]["listed"], data["direct_lane"]["session_attached"]), (True, True))
        self.assertTrue(cards["owner"]["listed"])
        queue = self.get("/api/queue")
        self.assertEqual((queue["direct_session"], queue["items"][0]["direct_session"]), (True, True))

    def test_attached_session_that_is_not_live_is_marked_down(self):
        """fix1: a session value on the direct lane that is not live: card listed, `session down`."""
        mid = self.direct(1)
        self.store.upsert_lane("owner", session_pointer=DEAD_ID)  # no live session file
        data = self.get("/api/lanes")
        card = next(card for card in data["lanes"] if card["lane"] == "owner")
        self.assertEqual((card["listed"], card["session_down"]), (True, True))
        self.assertEqual((data["direct_lane"]["session_attached"], data["direct_lane"]["session_down"]),
                         (True, True))
        self.assertFalse(next(c for c in data["lanes"] if c["lane"] == "support")["session_down"])
        queue = self.get("/api/queue")
        row = queue["items"][0]
        self.assertEqual((row["id"], row["direct_session"], row["direct_session_down"],
                          queue["direct_session_live"]), (mid, True, True, False))
        self.store.upsert_lane("owner", session_pointer=LIVE_ID)  # the live dummy session
        self.assertFalse(next(c for c in self.get("/api/lanes")["lanes"] if c["lane"] == "owner")["session_down"])
        row = self.get("/api/queue")["items"][0]
        self.assertEqual((row["direct_session"], row["direct_session_down"]), (True, False))
        script = (STATIC / "console.js").read_text(encoding="utf-8")
        for needle in ('sessionDown: "session down"', 'sessionDown: "세션 꺼짐"', "card.session_down"):
            self.assertIn(needle, script)

    def test_page_text_and_guards(self):
        script = (STATIC / "console.js").read_text(encoding="utf-8")
        for needle in ('directInQueue: (name) => "Operator items are in the queue"',
                       '"세션 있음"', '"레인 결정 필요 " + undecided', '"큐 (" + count + ")"',
                       "card.listed !== false", "&kind=", "isDirectRow(card.item) && (action === \"direct\""):
            self.assertIn(needle, script)
        css = (STATIC / "console.css").read_text(encoding="utf-8")
        self.assertIn(".badge.direct-kind", css)


class DigestAndCliTests(OperatorQueueCase):
    def test_digest_counts(self):
        self.direct(1)
        self.direct(2, state="acked")
        self.pending(3)
        self.assertEqual(queue_counts(self.store, "owner"), {"undecided": 1, "direct": 2})
        self.assertEqual(queue_counts(self.store, None), {"undecided": 1, "direct": 0})
        self.assertEqual(len(operator_queue(self.store, "owner")["direct"]), 2)
        self.assertEqual(digest_counts_text({"undecided": 1, "direct": 2}, "owner"),
                         "Operator queue: undecided=1, direct=2 (lane owner)")
        self.assertEqual(digest_counts_text({"undecided": 0, "direct": 0}),
                         "Operator queue: undecided=0, direct=0")

    def run_queue(self, kind, config, *extra):
        out = io.StringIO()
        args = cli.build_parser().parse_args(["queue"] + (["--kind", kind] if kind else []) + list(extra))
        cli.queue_command(args, self.store, config, out)
        return out.getvalue().splitlines()

    def test_cli_queue_kind_with_config(self):
        direct = self.direct(1)
        pending = self.pending(2, suggestion="support")
        lines = self.run_queue(None, self.config)
        self.assertEqual(lines[0], f"{pending} support dummy reason dummy-general Dummy_Author")
        self.assertEqual(lines[1], f"{direct} direct:owner assigned dummy-general Dummy_Author")
        # fix1: the default output has no counts line; --kind or --counts adds it.
        self.assertEqual(len(lines), 2)
        counts = "# Operator queue: undecided=1, direct=1 (lane owner)"
        self.assertEqual(self.run_queue(None, self.config, "--counts"), lines + [counts])
        self.assertEqual(self.run_queue("undecided", self.config), lines[:1] + [counts])
        self.assertEqual(self.run_queue("direct", self.config), lines[1:2] + [counts])
        self.assertEqual(self.run_queue("all", self.config), lines + [counts])

    def test_cli_queue_keeps_the_100_row_cap(self):
        for offset in range(1, 103):
            self.pending(offset)
        lines = self.run_queue(None, self.config)
        self.assertEqual(len(lines), 100)
        self.assertEqual(self.run_queue("undecided", self.config)[-1],
                         "# Operator queue: undecided=102, direct=0 (lane owner)")
        with self.assertRaises(cli.BadInput):
            cli.build_parser().parse_args(["queue", "--kind", "other"])


class CliWithoutConfigTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        for lane in ("owner-direct", "support"):
            self.store.upsert_lane(lane)
        self.store.messages["slack:CDUMMY:1"] = {"id": "slack:CDUMMY:1", "channel": "CDUMMY", "ts": "1",
                                                 "lane": None, "state": "human_pending",
                                                 "received_at": "2026-11-13T00:00:01+00:00"}
        self.store.messages["slack:CDUMMY:2"] = {"id": "slack:CDUMMY:2", "channel": "CDUMMY", "ts": "2",
                                                 "lane": "owner-direct", "state": "acked",
                                                 "received_at": "2026-11-13T00:00:02+00:00"}

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["--inbox-root", "unused"] + list(args), store=self.store, stdout=out, stderr=err)
        return code, out.getvalue().splitlines(), err.getvalue()

    def test_default_direct_lane_from_the_ledger(self):
        code, lines, err = self.run_cli("queue")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(lines, ["slack:CDUMMY:1 none none CDUMMY -",
                                 "slack:CDUMMY:2 direct:owner-direct acked CDUMMY -"])
        code, lines, _ = self.run_cli("queue", "--counts")
        self.assertEqual(lines[-1], "# Operator queue: undecided=1, direct=1 (lane owner-direct)")
        code, lines, _ = self.run_cli("queue", "--kind", "direct")
        self.assertEqual(lines[0], "slack:CDUMMY:2 direct:owner-direct acked CDUMMY -")
        self.assertEqual(self.run_cli("queue", "--kind", "bad")[0], 2)

    def test_no_direct_lane_in_the_ledger(self):
        self.store.lanes.pop("owner-direct")
        code, lines, _ = self.run_cli("queue", "--kind", "all")
        self.assertEqual(lines, ["slack:CDUMMY:1 none none CDUMMY -",
                                 "# Operator queue: undecided=1, direct=0"])


class DigestLineTests(unittest.TestCase):
    """fix1: a human queue digest ends with the operator queue counts line."""

    def digest(self, direct_lane):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p25-digest-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        config = replace(fixture_config(root), human_queue=HumanQueue(digest_seconds=10))
        config = replace(config, console=replace(config.console, direct_lane=direct_lane))
        store = open_store(config.ledger.path, store_body=False)
        self.addCleanup(store.close)
        clock = lambda: 1000.0  # noqa: E731
        notifiers = [Mock(), Mock(), FileDropNotifier(root / "human", {"ids": "{ids}"})]
        pipeline = Pipeline(config, store, NullSuggester(), notifiers,
                            WakePolicyEngine(config.wake_policy, clock), clock)
        pipeline.handle_event(event(user="U0000000091", text="plain"))  # immediate attention
        pipeline.handle_event(event(2, text="plain"))
        seeded = seed(store, "1000.000009", received=stamp(9))
        store.set_route(seeded, "owner", "rule:dummy")  # one open direct-lane item
        pipeline.flush(1010)
        bodies = {}
        for path in sorted((root / "human").glob("*.json")):
            body = json.loads(path.read_text(encoding="utf-8"))["body"]
            bodies["digest" if "digest" in body else "notice"] = body
        return bodies

    def test_digest_ends_with_counts_and_is_omitted_without_direct_lane(self):
        bodies = self.digest("owner")
        self.assertEqual(bodies["digest"].splitlines()[-1],
                         "Operator queue: undecided=2, direct=1 (lane owner)")
        self.assertNotIn("Operator queue:", bodies["notice"])  # immediate notices unchanged
        self.assertNotIn("Operator queue:", self.digest("owner-direct")["digest"])


if __name__ == "__main__":
    unittest.main()
