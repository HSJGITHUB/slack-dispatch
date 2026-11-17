"""P27: an owner reply also closes the direct-lane items of its thread (dummy ids only)."""
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import yaml

from slack_dispatch.config.schema import Lane, OwnerResolver as OwnerSettings
from slack_dispatch.inbox.cli import main as cli_main
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline, owner_reply_targets, queue_human_notice
from slack_dispatch.router.classifier import NullSuggester
from tests.fakes.fake_store import FakeStore
from tests.test_pipeline_integration import SAMPLE, event, fixture_config

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "registry_sample.json"
CHANNEL = "C0000000002"
OWNER = "U0000000093"  # always_human.owner_user of the sample config
DIRECT = "owner"  # console.direct_lane of the sample config
ROOT = "1794528010.000100"
REPLY_TS = "1794528010.000500"
LANES = (Lane("owner", None, False), Lane("support", None, False), Lane("content", None, True))


class ThreadSeed:
    """Seeds one thread: a human_pending root, two direct-lane assigned replies, one
    direct-lane acked reply and one other-lane reply before the owner reply at REPLY_TS,
    plus a direct-lane and a human_pending reply after it and a direct item elsewhere."""

    def seed_message(self, message_ts, *, thread=ROOT, channel=CHANNEL):
        self.store.record_event(dict(source="slack", event_id="EvDUMMY" + channel + message_ts,
                                     event_type="message", channel=channel, channel_type="channel",
                                     user="U0000000088", ts=message_ts,
                                     thread_ts=None if thread == message_ts else thread,
                                     is_thread_reply=thread != message_ts, has_files=False,
                                     received_at="2026-11-13T00:00:00+00:00", text="DUMMY BODY"))
        return f"slack:{channel}:{message_ts}"

    def pending(self, message_ts, **kwargs):
        mid = self.seed_message(message_ts, **kwargs)
        self.store.set_state(mid, "human_pending", note="none")
        return mid

    def routed(self, message_ts, lane, state="assigned", **kwargs):
        mid = self.seed_message(message_ts, **kwargs)
        self.store.set_route(mid, lane, "rule:dummy")
        if state != "assigned":
            self.store.set_state(mid, state)
        return mid

    def seed_thread(self):
        self.ids = dict(
            pending=self.pending(ROOT, thread=ROOT),
            direct_one=self.routed("1794528010.000200", DIRECT),
            direct_two=self.routed("1794528010.000300", DIRECT),
            direct_acked=self.routed("1794528010.000400", DIRECT, state="acked"),
            other_lane=self.routed("1794528010.000450", "support"),
            later_direct=self.routed("1794528010.000900", DIRECT),
            later_pending=self.pending("1794528010.000950"),
            elsewhere=self.routed("1794528020.000100", DIRECT, thread="1794528020.000100"),
        )
        for mid in self.ids.values():
            queue_human_notice(self.config, self.store, mid, now=1.0)
        return self.ids

    def states(self):
        return {name: (self.store.get_message(mid)["state"], self.store.get_message(mid)["note"])
                for name, mid in self.ids.items()}

    def notices(self, name):
        return self.store._rows("SELECT * FROM human_notices WHERE message_id=?", (self.ids[name],))

    def assert_untouched(self):
        states = self.states()
        self.assertEqual(states["pending"], ("human_pending", "none"))
        for name in ("direct_one", "direct_two", "other_lane", "later_direct", "elsewhere"):
            self.assertEqual(states[name][0], "assigned", name)
        self.assertEqual(states["direct_acked"][0], "acked")
        self.assertEqual(states["later_pending"], ("human_pending", "none"))

    def assert_closed(self):
        states = self.states()
        for name in ("pending", "direct_one", "direct_two", "direct_acked"):
            self.assertEqual(states[name], ("done", "owner_reply"), name)
            self.assertEqual(self.notices(name), [], name)
        self.assertEqual(states["other_lane"], ("assigned", None))
        self.assertEqual(states["later_direct"], ("assigned", None))
        self.assertEqual(states["later_pending"], ("human_pending", "none"))
        self.assertEqual(states["elsewhere"], ("assigned", None))
        for name in ("other_lane", "later_direct", "later_pending", "elsewhere"):
            self.assertTrue(self.notices(name), name)


class PipelineOwnerReplyTests(ThreadSeed, unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p27-pipeline-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        base = fixture_config(self.root)
        self.config = replace(base, lanes=LANES,
                              owner_resolver=OwnerSettings(enabled=True, registry_path=str(FIXTURE)),
                              human_queue=replace(base.human_queue, mode="digest"))
        self.assertEqual(self.config.console.direct_lane, DIRECT)
        self.store = LedgerStore(self.config.ledger.path)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.config.lanes:
            self.store.upsert_lane(lane.id)
        self.now = 1000.0
        self.pipeline = self.make()
        self.seed_thread()

    def make(self, config=None):
        config = config or self.config
        notifiers = [FileInboxNotifier(config.inbox.root, self.store.get_message), mock.Mock(), mock.Mock()]
        return Pipeline(config, self.store, NullSuggester(), notifiers,
                        WakePolicyEngine(config.wake_policy, lambda: self.now), lambda: self.now)

    def owner_reply(self, number=1, reply_ts=REPLY_TS, thread=ROOT, **changes):
        changes.setdefault("text", "dummy owner reply")
        return self.pipeline.handle_event(event(number, ts=reply_ts, thread_ts=thread, user=OWNER,
                                                is_thread_reply=thread is not None, **changes))

    def test_owner_reply_closes_pending_and_direct_items_only(self):
        result = self.owner_reply()
        self.assertEqual(result.state, "done")
        self.assert_closed()
        self.assertEqual(self.pipeline.snapshot()["owner_reply_closed"], 4)
        exported = (self.root / "state" / "human_queue.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(self.ids["pending"], exported)
        self.assertIn(self.ids["later_pending"], exported)
        # A second owner reply finds nothing earlier left to close.
        self.owner_reply(2, "1794528010.000600")
        self.assertEqual(self.pipeline.snapshot()["owner_reply_closed"], 4)

    def test_catchup_owner_reply_closes_the_same_items(self):
        self.owner_reply(origin="reconcile")
        self.assert_closed()
        self.assertEqual(self.pipeline.snapshot()["owner_reply_closed"], 4)

    def test_owner_root_post_closes_nothing(self):
        self.owner_reply(reply_ts="1794528030.000100", thread=None)
        self.assert_untouched()
        self.assertEqual(self.pipeline.snapshot()["owner_reply_closed"], 0)

    def test_switch_off_or_resolver_disabled_leaves_everything(self):
        self.pipeline = self.make(replace(self.config, owner_resolver=replace(
            self.config.owner_resolver, close_queue_on_owner_reply=False)))
        self.owner_reply()
        self.assert_untouched()
        self.pipeline = self.make(replace(self.config, owner_resolver=OwnerSettings()))
        self.owner_reply(2, "1794528010.000600")
        self.assert_untouched()

    def test_without_a_configured_direct_lane_only_human_pending_items_close(self):
        config = replace(self.config, console=replace(self.config.console, direct_lane="owner-direct"))
        self.pipeline = self.make(config)
        self.owner_reply()
        states = self.states()
        self.assertEqual(states["pending"], ("done", "owner_reply"))
        for name in ("direct_one", "direct_two", "other_lane"):
            self.assertEqual(states[name][0], "assigned", name)
        self.assertEqual(states["direct_acked"][0], "acked")
        self.assertEqual(self.pipeline.snapshot()["owner_reply_closed"], 1)

    def test_store_without_the_new_query_falls_back_to_pending_in_thread(self):
        store = FakeStore()
        self.assertEqual(owner_reply_targets(self.config, store, "slack", CHANNEL, ROOT, REPLY_TS), [])


class SentOperatorReplyTests(ThreadSeed, unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p27-cli-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        environ = {key: value for key, value in os.environ.items()
                   if key not in ("SLACK_DISPATCH_SESSION", "CLAUDE_SESSION_ID", "SLACK_DISPATCH_CONFIG")}
        patcher = mock.patch.dict(os.environ, environ, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.data = yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))
        self.data["owner_resolver"] = {"enabled": True, "registry_path": str(FIXTURE)}
        self.data["service"]["state_dir"] = str(self.root / "state")
        self.config_path = self.root / "config.yaml"
        self.write_config()
        self.config = fixture_config(self.root)
        self.store = LedgerStore(self.root / "ledger.sqlite3", store_body=True)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in ("owner", "support", "content"):
            self.store.upsert_lane(lane)
        self.seed_thread()

    def write_config(self):
        self.config_path.write_text(yaml.safe_dump(self.data), encoding="utf-8")

    def sent(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        args = ["--inbox-root", str(self.root / "inbox"), "--config", str(self.config_path), "sent", *argv]
        code = cli_main(args, store=self.store, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_operator_session_reply_closes_like_a_live_owner_reply(self):
        code, out, err = self.sent(CHANNEL, REPLY_TS, "--lane", DIRECT, "--thread", ROOT)
        self.assertEqual((code, err), (0, ""))
        closed = [self.ids[name] for name in ("pending", "direct_one", "direct_two", "direct_acked")]
        self.assertIn("owner_reply closed=4 " + ",".join(closed), out.splitlines())
        self.assert_closed()
        exported = [json.loads(line) for line in
                    (self.root / "state" / "human_queue.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["id"] for row in exported], [self.ids["later_pending"]])
        # Recording the same send again changes nothing.
        code, out, _ = self.sent(CHANNEL, REPLY_TS, "--lane", DIRECT, "--thread", ROOT)
        self.assertEqual((code, out.strip()), (0, f"sent {CHANNEL} {REPLY_TS} already recorded"))

    def test_other_lane_root_post_and_switches_close_nothing(self):
        code, out, _ = self.sent(CHANNEL, REPLY_TS, "--lane", "support", "--thread", ROOT)
        self.assertEqual(code, 0)
        self.assertNotIn("owner_reply", out)
        code, out, _ = self.sent(CHANNEL, "1794528030.000100", "--lane", DIRECT)  # a root post
        self.assertEqual(code, 0)
        self.assertNotIn("owner_reply", out)
        self.data["owner_resolver"]["close_queue_on_owner_reply"] = False
        self.write_config()
        code, out, _ = self.sent(CHANNEL, "1794528010.000600", "--lane", DIRECT, "--thread", ROOT)
        self.assertEqual(code, 0)
        self.assertNotIn("owner_reply", out)
        self.data["owner_resolver"] = {"enabled": False, "registry_path": str(FIXTURE)}
        self.write_config()
        code, out, _ = self.sent(CHANNEL, "1794528010.000700", "--lane", DIRECT, "--thread", ROOT)
        self.assertEqual(code, 0)
        self.assertNotIn("owner_reply", out)
        self.assert_untouched()
        self.assertFalse((self.root / "state" / "human_queue.jsonl").exists())

    def test_without_config_nothing_closes(self):
        out, err = io.StringIO(), io.StringIO()
        code = cli_main(["--inbox-root", str(self.root / "inbox"), "sent", CHANNEL, REPLY_TS,
                         "--lane", DIRECT, "--thread", ROOT], store=self.store, stdout=out, stderr=err)
        self.assertEqual(code, 0)
        self.assertNotIn("owner_reply", out.getvalue())
        self.assert_untouched()


if __name__ == "__main__":
    unittest.main()
