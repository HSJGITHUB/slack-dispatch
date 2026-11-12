import tempfile
import unittest
from pathlib import Path

from slack_dispatch.notify.file_inbox import FileInboxNotifier
from slack_dispatch.notify.wake import Batch, Defer, Deliver, WakePolicyEngine


class WakeTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.policy = {"enabled": True, "debounce_seconds": 90, "storm_per_hour": 30,
                       "batch_seconds": 600, "state_dir": self.temp.name,
                       "immediate_users": ["UDUMMY_FAST"], "owner_user": "UDUMMY_OWNER"}
        self.engine = WakePolicyEngine(self.policy, lambda: self.now)

    def offer(self, mid, author="UDUMMY", lane="support", **extra):
        return self.engine.offer(lane, dict(id=mid, **extra), author)

    def test_immediate_debounce_and_boundary(self):
        self.assertIsInstance(self.offer("m1"), Deliver)
        self.now = 1
        self.assertIsInstance(self.offer("m2"), Defer)
        self.assertIsInstance(self.offer("m3"), Defer)
        self.assertIsInstance(self.offer("m4", author="OTHER"), Deliver)
        self.assertIsInstance(self.offer("m5", lane="other"), Deliver)
        self.assertEqual(self.engine.flush(89), [])
        self.assertEqual(self.engine.flush(90), [("support", ["m2", "m3"])])
        self.assertEqual(self.engine.flush(91), [])
        self.now = 180
        self.assertIsInstance(self.offer("m6"), Deliver)

    def test_immediate_users_and_mentions(self):
        self.offer("m1", author="UDUMMY_FAST")
        self.assertIsInstance(self.offer("m2", author="UDUMMY_FAST"), Deliver)
        self.offer("m3")
        self.assertIsInstance(self.offer("m4", text="hello <@UDUMMY_OWNER>"), Deliver)
        self.assertIsInstance(self.offer("m5", mentions=["UDUMMY_OWNER"]), Deliver)
        self.assertIsInstance(self.offer("m6", text="UDUMMY_OWNER_suffix"), Defer)

    def test_storm_batch_and_recovery(self):
        self.policy["storm_per_hour"] = 3
        self.offer("m1")
        self.offer("m2")
        decision = self.offer("m3")
        self.assertIsInstance(decision, Batch)
        self.assertEqual(decision.notice.count, 3)
        self.assertIsNone(self.offer("m4").notice)
        self.assertEqual(len(self.engine.notices), 1)
        self.assertEqual(self.engine.flush(90), [])
        self.assertEqual(self.engine.flush(600), [("support", ["m2", "m3", "m4"])])
        self.now = 601
        self.assertIsInstance(self.offer("m5", author="UDUMMY_FAST"), Batch)
        self.assertEqual(self.engine.flush(1199), [])
        self.assertEqual(self.engine.flush(1200), [("support", ["m5"])])
        self.now = 3600
        self.assertIsInstance(self.offer("m6"), Deliver)
        self.assertIsInstance(self.offer("m7", lane="other"), Deliver)

    def test_global_switch_and_file_delivery(self):
        self.policy["enabled"] = False
        self.assertEqual(self.offer("m1").reason, "disabled")
        FileInboxNotifier(self.temp.name).notify("support", ["slack:CDUMMY:1"])
        self.assertTrue((Path(self.temp.name) / "support.jsonl").exists())
        self.policy["enabled"] = True
        self.offer("m2")
        self.offer("m3")
        (Path(self.temp.name) / "wake.off").touch()
        self.assertEqual(self.offer("m4").reason, "disabled")
        self.assertEqual(self.engine.flush(100), [])
        (Path(self.temp.name) / "wake.off").rename(Path(self.temp.name) / "wake.disabled")
        self.assertEqual(self.engine.flush(100), [("support", ["m3"])])

    def test_disabled_events_accumulate_storm_count(self):
        self.policy["storm_per_hour"] = 3
        self.policy["enabled"] = False
        self.assertEqual(self.offer("m1").reason, "disabled")
        self.assertEqual(self.offer("m2").reason, "disabled")
        self.policy["enabled"] = True
        decision = self.offer("m3")
        self.assertIsInstance(decision, Batch)
        self.assertEqual(decision.notice.count, 3)
