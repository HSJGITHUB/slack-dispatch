"""P24: edits never undo a reroute, and a reply reroute never rebinds the thread.

Dummy ids only. Every ledger, inbox and journal file lives in a temporary directory.
"""
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from slack_dispatch.console import actions
from slack_dispatch.inbox import cli
from slack_dispatch.ingest.normalize import comparable_text, message_body, normalize_slack_event
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline, cosmetic_edit, edit_text_changed
from slack_dispatch.router.classifier import NullSuggester
from tests.test_console_app import ts as console_ts
from tests.test_console_undo import UndoCase
from tests.test_pipeline_integration import fixture_config

CHANNEL = "C0000000002"
AUTHOR = "U0000000088"
ROOT_TS, REPLY_TS, OTHER_TS = "1000.000001", "1000.000002", "1000.000003"
RECEIVED = "2026-11-16T07:43:41+00:00"
URL = "https://example.invalid/dummy"
LINK_BLOCKS = [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
    {"type": "text", "text": "see "},
    {"type": "link", "url": URL, "text": "dummy page"},
    {"type": "text", "text": " & <@U0000000091> please"}]}]}]
# The raw mrkdwn `text` of the same message, as Slack sends it next to the blocks.
LINK_RAW = f"see <{URL}|dummy page> &amp; <@U0000000091|dummy> please"


def slack_payload(number, ts, *, thread_ts=None, text=LINK_RAW, blocks=LINK_BLOCKS,
                  subtype=None):
    """A Slack Events API payload; a message_changed edit wraps the message and adds an
    attachment (a link preview)."""
    message = dict(type="message", user=AUTHOR, ts=ts, text=text)
    if thread_ts:
        message["thread_ts"] = thread_ts
    if blocks is not None:
        message["blocks"] = blocks
    if subtype == "message_changed":
        message["attachments"] = [{"title": "dummy preview", "from_url": URL}]
        inner = dict(type="message", subtype=subtype, channel=CHANNEL, channel_type="channel",
                     message=message)
    else:
        inner = dict(message, channel=CHANNEL, channel_type="channel")
    return {"event_id": f"EvDUMMYP24{number}", "event": inner}


def normalized(number, ts, **changes):
    event = normalize_slack_event(slack_payload(number, ts, **changes), RECEIVED, store_body=True)
    event["origin"] = "socket"
    return event


class EditGuardCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p24-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = fixture_config(self.root)
        self.now = 1000.0
        self.store = LedgerStore(self.config.ledger.path, store_body=True)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id, session_pointer=lane.session, paused=lane.paused)
        self.inbox = self.config.inbox.root

    def make_pipeline(self, policy=None):
        if policy is not None:
            self.config = replace(self.config, wake_policy=replace(self.config.wake_policy,
                                                                   followup=policy))
        clock = lambda: self.now  # noqa: E731
        self.lane, self.human = Mock(), Mock()
        notifiers = [FileInboxNotifier(self.inbox, self.store.get_message), self.lane, self.human]
        engine = WakePolicyEngine(self.config.wake_policy, clock)
        self.offers = Mock(side_effect=engine.offer)
        engine.offer = self.offers
        self.pipeline = Pipeline(self.config, self.store, NullSuggester(), notifiers, engine, clock)
        return self.pipeline

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["--inbox-root", self.inbox, *argv], store=self.store, stdout=out,
                        stderr=err)
        self.assertEqual(code, 0, err.getvalue())
        return out.getvalue()

    def records(self, lane):
        path = Path(self.inbox) / f"{lane}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def binding(self, thread_ts=ROOT_TS):
        return self.store.lookup_binding("slack", CHANNEL, thread_ts)

    def row(self, mid):
        return self.store.get_message(mid, include_body=True)

    def thread_in_support(self):
        """A root bound to `support` (lane A) and a reply routed there by thread_inherit."""
        self.make_pipeline()
        root = self.pipeline.handle_event(normalized(1, ROOT_TS, text="[support] dummy design thread",
                                                     blocks=None))
        self.assertEqual((root.state, root.lane), ("assigned", "support"))
        self.now += 100
        reply = self.pipeline.handle_event(normalized(2, REPLY_TS, thread_ts=ROOT_TS))
        self.assertEqual((reply.state, reply.lane), ("assigned", "support"))
        self.assertEqual(self.row(reply.message_id)["body"],
                         f"see {URL} & <@U0000000091> please")
        self.now += 100
        return root.message_id, reply.message_id

    def counts(self):
        return (self.offers.call_count, self.lane.notify.call_count,
                len(self.records("support")), len(self.records("owner")))


class EditAfterRerouteTests(EditGuardCase):
    def test_link_preview_edit_after_cli_reroute_keeps_the_item_in_the_new_lane(self):
        """The 2026-11-16 evidence: reply in lane A, CLI reroute to B, B reads it, the
        operator points the thread back at A, then Slack sends a link preview edit whose
        message carries no blocks (raw mrkdwn only)."""
        _, reply = self.thread_in_support()
        self.cli("reroute", reply, "--to", "owner")
        self.assertEqual((self.row(reply)["lane"], self.row(reply)["route_reason"]),
                         ("owner", "reroute:support"))
        self.cli("ack", reply)
        self.store.bind_thread("slack", CHANNEL, ROOT_TS, "support")  # rebound by hand
        before = self.counts()
        edit = normalized(3, REPLY_TS, thread_ts=ROOT_TS, blocks=None, subtype="message_changed")
        self.assertEqual(edit["text"], LINK_RAW)
        result = self.pipeline.handle_event(edit)
        row = self.row(reply)
        self.assertEqual((result.state, result.lane), ("acked", "owner"))
        self.assertEqual((row["lane"], row["state"], row["route_reason"]),
                         ("owner", "acked", "reroute:support"))
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.binding(), "support")
        self.assertIsNotNone(row["edited_at"])
        self.assertEqual(row["body"], LINK_RAW)
        self.assertEqual((self.pipeline.edits_ignored, self.pipeline.edits_woken), (1, 0))

    def test_link_preview_edit_while_assigned_after_reroute(self):
        _, reply = self.thread_in_support()
        self.cli("reroute", reply, "--to", "owner")
        before = self.counts()
        for number, blocks in ((3, LINK_BLOCKS), (4, None)):
            self.now += 10
            self.pipeline.handle_event(normalized(number, REPLY_TS, thread_ts=ROOT_TS,
                                                  blocks=blocks, subtype="message_changed"))
        row = self.row(reply)
        self.assertEqual((row["lane"], row["state"], row["route_reason"]),
                         ("owner", "assigned", "reroute:support"))
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.binding(), "support")

    def test_text_edit_while_assigned_after_reroute_is_ledger_only(self):
        _, reply = self.thread_in_support()
        self.cli("reroute", reply, "--to", "owner")
        before = self.counts()
        self.pipeline.handle_event(normalized(3, REPLY_TS, thread_ts=ROOT_TS, blocks=None,
                                              text="a real correction", subtype="message_changed"))
        row = self.row(reply)
        self.assertEqual((row["lane"], row["state"], row["route_reason"], row["body"]),
                         ("owner", "assigned", "reroute:support", "a real correction"))
        self.assertEqual(self.counts(), before)

    def test_text_edit_of_acked_rerouted_item_wakes_its_current_lane(self):
        _, reply = self.thread_in_support()
        self.cli("reroute", reply, "--to", "owner")
        self.cli("ack", reply)
        support_records = len(self.records("support"))
        owner_records = len(self.records("owner"))
        offers = self.offers.call_count
        self.pipeline.handle_event(normalized(3, REPLY_TS, thread_ts=ROOT_TS, blocks=None,
                                              text="a real correction", subtype="message_changed"))
        row = self.row(reply)
        self.assertEqual((row["lane"], row["state"], row["route_reason"]),
                         ("owner", "assigned", "reroute:support"))
        self.assertEqual(self.offers.call_count, offers + 1)
        self.assertEqual(self.offers.call_args.args[0], "owner")
        self.assertEqual(len(self.records("support")), support_records)
        records = self.records("owner")
        self.assertEqual(len(records), owner_records + 1)
        self.assertEqual((records[-1]["id"], records[-1]["kind"], records[-1]["followup"]),
                         (reply, "followup", True))
        self.assertEqual(self.binding(), "support")
        self.assertEqual(self.pipeline.edits_woken, 1)

    def test_done_item_assigned_by_hand_reopens_in_its_lane(self):
        _, reply = self.thread_in_support()
        self.cli("assign", reply, "content")  # human:cli, a lane other than the binding
        self.cli("done", reply)
        self.pipeline.handle_event(normalized(3, REPLY_TS, thread_ts=ROOT_TS, blocks=None,
                                              text="reopened by a change", subtype="message_changed"))
        row = self.row(reply)
        self.assertEqual((row["lane"], row["state"], row["route_reason"]),
                         ("content", "assigned", "human:cli"))
        self.assertEqual(self.records("content")[-1]["kind"], "followup")
        self.assertEqual(self.binding(), "support")

    def test_policy_always_redelivers_to_the_current_lane(self):
        _, reply = self.thread_in_support()
        self.config = replace(self.config, wake_policy=replace(self.config.wake_policy,
                                                               followup="always"))
        self.make_pipeline()
        self.cli("reroute", reply, "--to", "owner")
        self.pipeline.handle_event(normalized(3, REPLY_TS, thread_ts=ROOT_TS,
                                              subtype="message_changed"))
        row = self.row(reply)
        self.assertEqual((row["lane"], row["state"], row["route_reason"]),
                         ("owner", "assigned", "reroute:support"))
        self.assertEqual(self.offers.call_args.args[0], "owner")
        self.assertEqual(self.records("owner")[-1]["kind"], "followup")

    def test_blocks_only_edit_with_empty_text_is_no_change(self):
        """fix1: an edit whose message carries blocks and an empty `text`."""
        _, reply = self.thread_in_support()
        self.cli("reroute", reply, "--to", "owner")
        self.cli("ack", reply)
        before = self.counts()
        edit = normalized(3, REPLY_TS, thread_ts=ROOT_TS, text="", subtype="message_changed")
        self.assertEqual(edit["text"], f"see {URL} & <@U0000000091> please")
        self.pipeline.handle_event(edit)
        row = self.row(reply)
        self.assertEqual((row["lane"], row["state"], row["route_reason"]),
                         ("owner", "acked", "reroute:support"))
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.pipeline.edits_ignored, 1)

    def test_owner_post_and_prehandled_items_with_a_lane_stay_closed(self):
        """fix1: edits of service-closed items that hold a lane are recorded only, text
        change or not."""
        _, reply = self.thread_in_support()
        other = self.pipeline.handle_event(normalized(5, OTHER_TS, thread_ts=ROOT_TS, blocks=None,
                                                      text="another reply")).message_id
        self.store.set_route(reply, "support", "owner_post", state="done")
        self.store.set_state(reply, "done", note="owner_post")
        self.store.set_route(other, "support", "prehandled", state="done")
        self.store.set_state(other, "done", note="prehandled:owner_reply")
        routed = self.pipeline.counters["routed"]
        before = self.counts()
        number = 10
        for mid, ts in ((reply, REPLY_TS), (other, OTHER_TS)):
            for blocks, text in ((None, LINK_RAW), (None, "a real correction")):
                number += 1
                self.now += 1000
                self.pipeline.handle_event(normalized(number, ts, thread_ts=ROOT_TS, blocks=blocks,
                                                      text=text, subtype="message_changed"))
            row = self.row(mid)
            self.assertEqual((row["lane"], row["state"]), ("support", "done"), mid)
        self.assertEqual(self.row(reply)["route_reason"], "owner_post")
        self.assertEqual(self.row(other)["route_reason"], "prehandled")
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.pipeline.counters["routed"], routed)
        self.assertEqual((self.pipeline.edits_woken, self.pipeline.edits_ignored), (0, 4))

    def test_redelivered_edit_counts_as_routed(self):
        _, reply = self.thread_in_support()
        self.cli("reroute", reply, "--to", "owner")
        self.cli("ack", reply)
        routed = self.pipeline.counters["routed"]
        self.pipeline.handle_event(normalized(3, REPLY_TS, thread_ts=ROOT_TS, blocks=None,
                                              text="a real correction", subtype="message_changed"))
        self.assertEqual(self.pipeline.counters["routed"], routed + 1)

    def test_record_event_reports_the_row_before_the_edit(self):
        _, reply = self.thread_in_support()
        self.cli("reroute", reply, "--to", "owner")
        self.cli("ack", reply)
        result = self.store.record_event(normalized(3, REPLY_TS, thread_ts=ROOT_TS,
                                                    subtype="message_changed"))
        self.assertEqual((result.previous_state, result.previous_lane), ("acked", "owner"))
        row = self.row(reply)
        self.assertEqual((row["state"], row["lane"], row["route_reason"]),
                         ("acked", "owner", "reroute:support"))
        fresh = self.store.record_event(normalized(4, OTHER_TS, subtype="message_changed"))
        self.assertEqual((fresh.previous_state, fresh.previous_lane), (None, None))


class HumanPendingEditTests(EditGuardCase):
    def test_link_preview_edit_of_a_queued_item_is_ledger_only(self):
        self.make_pipeline()
        queued = self.pipeline.handle_event(normalized(1, ROOT_TS))
        self.assertEqual(queued.state, "human_pending")
        notices, routed = self.human.notify.call_count, self.pipeline.counters["routed"]
        self.now += 100
        edited = self.pipeline.handle_event(normalized(2, ROOT_TS, blocks=None,
                                                       subtype="message_changed"))
        self.assertEqual((edited.state, edited.lane), ("human_pending", None))
        self.assertEqual((self.human.notify.call_count, self.pipeline.counters["routed"]),
                         (notices, routed))
        self.assertEqual(self.offers.call_count, 0)
        self.assertEqual(self.pipeline.edits_ignored, 1)
        self.assertEqual(self.row(queued.message_id)["body"], LINK_RAW)

    def test_text_edit_of_a_queued_item_still_routes(self):
        self.make_pipeline()
        queued = self.pipeline.handle_event(normalized(1, ROOT_TS))
        self.now += 100
        edited = self.pipeline.handle_event(normalized(2, ROOT_TS, blocks=None,
                                                       text="[support] now tagged",
                                                       subtype="message_changed"))
        self.assertEqual((edited.state, edited.lane), ("assigned", "support"))
        self.assertEqual(self.records("support")[-1]["id"], queued.message_id)


class ComparableTextTests(unittest.TestCase):
    def test_raw_mrkdwn_compares_equal_to_the_block_rendering(self):
        rendered = f"see {URL} & <@U0000000091> please"
        self.assertEqual(comparable_text(LINK_RAW), comparable_text(rendered))
        self.assertFalse(edit_text_changed(rendered, LINK_RAW))
        self.assertFalse(edit_text_changed(LINK_RAW, rendered))
        self.assertFalse(edit_text_changed("<#C0000000001>  hi", "<#C0000000001|dummy> hi\n"))
        self.assertFalse(edit_text_changed("bold and code", "*bold* and `code`"))
        self.assertFalse(edit_text_changed("item one\nitem two", "• item one\n• item two"))
        self.assertFalse(edit_text_changed(f"{URL}", f"<{URL}>"))

    def test_markers_inside_words_stay(self):
        """fix1: only formatting markers at word boundaries drop."""
        self.assertTrue(edit_text_changed("5*3", "53"))
        self.assertTrue(edit_text_changed("snake_case", "snakecase"))
        self.assertTrue(edit_text_changed("a~b", "ab"))
        self.assertFalse(edit_text_changed("bold italic", "*_bold italic_*"))
        self.assertFalse(edit_text_changed("x\nfenced", "x\n```fenced```"))
        self.assertEqual(comparable_text("5 * 3 and snake_case"), "5 * 3 and snake_case")
        # A link preview edit without blocks is still no text change.
        self.assertFalse(edit_text_changed(f"see {URL} & <@U0000000091> please", LINK_RAW))

    def test_strike_and_quote_are_no_text_change_known_gap(self):
        """P38 fix1 evidence (known gap, not fixed): message_body renders rich text without
        styles or quote markers, and comparable_text drops `~...~` and a line-leading `>` or
        `&gt;` from raw mrkdwn. So neither path sees strike-through or a quote added or
        removed. Fixing it needs message_body to render the markers, which changes the stored
        body and the router input; see the P38 FIX1 notes."""
        def blocks(section):
            return {"blocks": [{"type": "rich_text", "elements": [section]}]}

        plain = blocks({"type": "rich_text_section",
                        "elements": [{"type": "text", "text": "dummy yes"}]})
        struck = blocks({"type": "rich_text_section", "elements": [
            {"type": "text", "text": "dummy "},
            {"type": "text", "text": "yes", "style": {"strike": True}}]})
        quoted = blocks({"type": "rich_text_quote",
                         "elements": [{"type": "text", "text": "dummy yes"}]})
        self.assertEqual(message_body(plain), "dummy yes")
        self.assertEqual(message_body(struck), "dummy yes")
        self.assertEqual(message_body(quoted), "dummy yes")
        for old, new in (("dummy ~yes~", "dummy yes"), ("dummy yes", "dummy ~yes~"),
                         ("&gt; dummy yes", "dummy yes"), ("> dummy yes", "dummy yes")):
            with self.subTest(old=old, new=new):
                self.assertFalse(edit_text_changed(old, new))
        # The cosmetic rule keeps the markers (P38 fix1), but it only runs after a text change.
        self.assertFalse(cosmetic_edit("dummy ~yes~", "dummy yes"))
        self.assertFalse(cosmetic_edit("&gt; dummy yes", "dummy yes"))

    def test_real_changes_still_count(self):
        self.assertTrue(edit_text_changed("dummy text", "dummy text 2"))
        self.assertTrue(edit_text_changed(f"see {URL}", "see https://example.invalid/other"))
        self.assertTrue(edit_text_changed("<@U0000000091>", "<@U0000000092>"))
        self.assertTrue(edit_text_changed(None, "dummy"))
        self.assertTrue(edit_text_changed("dummy", None))


class RerouteBindingTests(EditGuardCase):
    def setUp(self):
        super().setUp()
        self.root_id, self.reply_id = self.thread_in_support()

    def other_reply(self, number=5, ts=OTHER_TS, text="another reply"):
        result = self.pipeline.handle_event(normalized(number, ts, thread_ts=ROOT_TS, blocks=None,
                                                       text=text))
        self.assertEqual(result.lane, "support")
        return result.message_id

    def test_reply_reroute_leaves_the_binding(self):
        self.cli("reroute", self.reply_id, "--to", "owner")
        self.assertEqual(self.binding(), "support")
        self.assertEqual(self.row(self.reply_id)["lane"], "owner")
        # A later reply still follows the thread's lane.
        later = self.other_reply()
        self.assertEqual(self.row(later)["route_reason"], "thread_inherit")
        self.assertEqual(self.row(self.root_id)["lane"], "support")

    def test_root_reroute_moves_the_binding_only(self):
        other = self.other_reply()
        self.cli("reroute", self.root_id, "--to", "owner")
        self.assertEqual(self.binding(), "owner")
        self.assertEqual(self.row(self.root_id)["lane"], "owner")
        self.assertEqual((self.row(self.reply_id)["lane"], self.row(other)["lane"]),
                         ("support", "support"))

    def test_thread_reroute_moves_binding_and_open_items(self):
        other = self.other_reply()
        closed = self.other_reply(6, "1000.000004", "a closed reply")
        self.cli("done", closed)
        elsewhere = self.other_reply(7, "1000.000005", "a reply in another lane")
        self.cli("reroute", elsewhere, "--to", "content")
        wakes_before = len(self.store._rows("SELECT * FROM wake_requests"))
        self.cli("reroute", self.reply_id, "--to", "owner", "--thread")
        self.assertEqual(self.binding(), "owner")
        for mid in (self.root_id, self.reply_id, other):
            self.assertEqual((self.row(mid)["lane"], self.row(mid)["route_reason"]),
                             ("owner", "reroute:support"), mid)
        self.assertEqual((self.row(closed)["lane"], self.row(closed)["state"]), ("support", "done"))
        self.assertEqual(self.row(elsewhere)["lane"], "content")
        owner_ids = [row["id"] for row in self.records("owner")]
        self.assertEqual(sorted(owner_ids), sorted([self.root_id, self.reply_id, other]))
        self.assertTrue(all(row["kind"] == "reroute" for row in self.records("owner")))
        wakes = self.store._rows("SELECT * FROM wake_requests")
        self.assertEqual(len(wakes) - wakes_before, 3)

    def test_assign_of_a_reply_binds_only_an_unbound_thread(self):
        self.cli("assign", self.reply_id, "content")
        self.assertEqual(self.binding(), "support")
        # A reply of a thread without a binding binds it.
        unbound = self.pipeline.handle_event(normalized(8, "1000.000011", thread_ts="1000.000010",
                                                        blocks=None, text="no tag here"))
        self.assertEqual(unbound.state, "human_pending")
        self.assertIsNone(self.binding("1000.000010"))
        self.cli("assign", unbound.message_id, "content")
        self.assertEqual(self.binding("1000.000010"), "content")

    def test_assign_of_a_root_rebinds(self):
        self.cli("assign", self.root_id, "content")
        self.assertEqual(self.binding(), "content")


class ConsoleRerouteParityTests(UndoCase):
    def binding(self, offset=1):
        return self.store.lookup_binding("slack", CHANNEL, console_ts(offset))

    def thread(self):
        root = self.lane_item(1)
        self.store.bind_thread("slack", CHANNEL, console_ts(1), "support")
        replies = []
        for offset in (2, 3):
            mid = actions_seed(self.store, offset)
            self.store.set_route(mid, "support", "thread_inherit")
            replies.append(mid)
        return root, replies

    def test_console_reply_reroute_leaves_the_binding(self):
        root, (reply, other) = self.thread()
        data = self.post("/api/lanes/support/actions", {"action": "reroute", "ids": [reply],
                                                        "lane": "content"})
        self.assertEqual(data["applied"], 1)
        self.assertEqual(self.binding(), "support")
        self.assertEqual((self.store.get_message(reply)["lane"],
                          self.store.get_message(other)["lane"],
                          self.store.get_message(root)["lane"]), ("content", "support", "support"))
        before = json.loads(self.store.get_console_action(data["undo"]["action_id"])["before"])
        self.assertNotIn("binding", before[0])

    def test_console_root_reroute_moves_the_binding(self):
        root, (reply, _) = self.thread()
        self.post("/api/lanes/support/actions", {"action": "reroute", "ids": [root],
                                                 "lane": "content"})
        self.assertEqual(self.binding(), "content")
        self.assertEqual(self.store.get_message(reply)["lane"], "support")

    def test_console_thread_flag_moves_the_thread(self):
        """fix1: `thread: true` in the lane action body."""
        root, (reply, other) = self.thread()
        data = self.post("/api/lanes/support/actions", {"action": "reroute", "ids": [reply],
                                                        "lane": "content", "thread": True})
        self.assertEqual(data["applied"], 1)
        self.assertEqual(self.binding(), "content")
        for mid in (root, reply, other):
            self.assertEqual(self.store.get_message(mid)["lane"], "content", mid)
        self.assertEqual(data["undo"]["count"], 3)
        # `thread` is ignored for other lane actions.
        third = actions_seed(self.store, 4)
        self.store.set_route(third, "content", "thread_inherit")
        self.post("/api/lanes/content/actions", {"action": "ack", "ids": [other], "thread": True})
        self.assertEqual((self.store.get_message(other)["state"],
                          self.store.get_message(third)["state"]), ("acked", "assigned"))

    def test_apply_thread_flag_matches_the_cli(self):
        root, (reply, other) = self.thread()
        journal = actions.Journal(self.store, "lane_reroute", lane="content")
        results = actions.apply(self.store, self.inbox, "reroute", [reply], lane="content",
                                guard=actions.lane_guard("support"), journal=journal, thread=True)
        self.assertEqual([row["ok"] for row in results], [True])
        self.assertEqual(self.binding(), "content")
        for mid in (root, reply, other):
            self.assertEqual(self.store.get_message(mid)["lane"], "content", mid)
        info = journal.finish(30, self.now)
        self.assertEqual(sorted(json.loads(self.store.get_console_action(info["action_id"])["ids"])),
                         sorted([root, reply, other]))
        self.undo(info["action_id"])
        self.assertEqual(self.binding(), "support")
        for mid in (root, reply, other):
            self.assertEqual(self.store.get_message(mid)["lane"], "support", mid)


def actions_seed(store, offset):
    from tests.test_console_app import stamp
    from tests.test_console_core import seed
    return seed(store, console_ts(offset), thread=console_ts(1), received=stamp(offset))


if __name__ == "__main__":
    unittest.main()
