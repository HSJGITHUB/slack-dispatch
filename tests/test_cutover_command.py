"""P12 item 1: `cutover LANE` closes only what the lane demonstrably saw."""
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest

from slack_dispatch.inbox.cli import main
from slack_dispatch.inbox.files import append, record
from slack_dispatch.ledger.store import LedgerStore

BASE = 1_800_000_000


def iso(seconds):
    return datetime.fromtimestamp(BASE + seconds, timezone.utc).isoformat()


class LedgerCase(unittest.TestCase):
    """A real SQLite ledger in a temporary folder with dummy lanes and ids."""

    lanes = ("owner", "support", "content")

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="dispatch-p12-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.inbox = self.root / "inbox"
        self.store = LedgerStore(self.root / "ledger.sqlite3", store_body=True)
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)
        for lane in self.lanes:
            self.store.upsert_lane(lane)
        self.count = 0

    def add(self, *, lane="support", state="assigned", received=0, assigned=None, ack=None,
            done=None, attention=None, note=None, reason="channel_default", channel="C0000000001",
            thread=None, author="U0000000088", body="dummy body", deliver=True):
        self.count += 1
        ts = f"{BASE + self.count}.000100"
        mid = self.store.record_event(dict(
            source="slack", event_id=f"EvP12{self.count:04d}", event_type="message", subtype=None,
            channel=channel, channel_type="channel", user=author, ts=ts, thread_ts=thread,
            is_thread_reply=bool(thread), has_files=False, received_at=iso(received),
            text=body)).message_id
        fields = {"state": state, "lane": lane, "route_reason": reason, "note": note,
                  "assigned_at": iso(received if assigned is None else assigned),
                  "ack_at": iso(ack) if ack is not None else None,
                  "done_at": iso(done) if done is not None else None, "attention": attention}
        self.store._update(mid, fields)
        if deliver and lane:
            append(self.inbox, lane, [record(dict(self.store.get_message(mid), id=mid), "new")])
        return mid

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = main(["--inbox-root", str(self.inbox), *args], store=self.store, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def records(self, lane="support"):
        path = self.inbox / (lane + ".jsonl")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def state(self, mid):
        row = self.store.get_message(mid)
        return row["state"], row["note"]


class CutoverTests(LedgerCase):
    def test_no_lane_activity_closes_nothing_and_redelivers_all(self):
        first = self.add(received=10)
        second = self.add(received=20, state="acked", ack=None)
        code, out, err = self.cli("cutover", "support", "--apply")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("lane_last_seen=none basis=lane activity", out)
        self.assertIn("closed=0 kept_open=2 redelivered=2 attention_kept=0 refused=0", out)
        self.assertIn(first + " kept_open attention=-", out)
        self.assertIn(second + " kept_open attention=-", out)
        self.assertEqual(self.state(first)[0], "assigned")
        backfill = [row["id"] for row in self.records() if row["kind"] == "backfill"]
        self.assertEqual(backfill, [first, second])

    def test_mixed_closes_seen_items_and_keeps_later_ones(self):
        self.add(state="done", received=0, done=500, note="handled by lane")
        seen = self.add(received=100)
        acked = self.add(state="acked", received=150, ack=200)
        later = self.add(received=600)
        late_assign = self.add(received=50, assigned=700)
        busy_thread = self.add(received=120)
        # A later delivery in the same thread and lane keeps the older item open as well.
        self.store._db.execute("CREATE TABLE IF NOT EXISTS service_deliveries (source TEXT NOT NULL, "
                               "channel TEXT NOT NULL, thread_ts TEXT NOT NULL, lane TEXT NOT NULL, "
                               "delivered_at REAL NOT NULL, PRIMARY KEY (source, channel, thread_ts, lane))")
        row = self.store.get_message(busy_thread)
        self.store._db.execute("INSERT INTO service_deliveries VALUES ('slack', ?, ?, 'support', ?)",
                               (row["channel"], row["ts"], BASE + 800.0))
        other_lane = self.add(lane="content", received=10)
        code, out, _ = self.cli("cutover", "support", "--apply", "--note", "dummy cutover")
        self.assertEqual(code, 0)
        self.assertIn(f"lane_last_seen={iso(500)}", out)
        self.assertIn("closed=2 kept_open=3 redelivered=3 attention_kept=0", out)
        self.assertEqual(self.state(seen), ("done", "cutover: dummy cutover"))
        self.assertEqual(self.state(acked), ("done", "cutover: dummy cutover"))
        for mid in (later, late_assign, busy_thread):
            self.assertEqual(self.state(mid)[0], "assigned")
            self.assertIn(mid + " kept_open", out)
        self.assertEqual(self.state(other_lane)[0], "assigned")
        self.assertEqual(sorted(row["id"] for row in self.records() if row["kind"] == "backfill"),
                         sorted([later, late_assign, busy_thread]))

    def test_attention_items_always_stay_open(self):
        self.add(state="done", received=0, done=900)
        mention = self.add(received=10, attention="owner_mention")
        human = self.add(received=20, state="acked", attention="always_human")
        code, out, _ = self.cli("cutover", "support", "--apply")
        self.assertEqual(code, 0)
        self.assertIn("closed=0 kept_open=2 redelivered=2 attention_kept=2", out)
        self.assertIn(mention + " kept_open attention=owner_mention", out)
        self.assertEqual(self.state(human)[0], "acked")

    def test_seen_before_override_accepts_iso_and_slack_ts(self):
        self.add(state="done", received=0, done=900)
        early = self.add(received=100)
        middle = self.add(received=300)
        code, out, _ = self.cli("cutover", "support", "--seen-before", iso(50))
        self.assertEqual(code, 0)
        self.assertIn("basis=seen-before", out)
        self.assertIn("closed=0 kept_open=2", out)
        code, out, _ = self.cli("cutover", "support", "--seen-before", f"{BASE + 200}.000000")
        self.assertIn("closed=1 kept_open=1", out)
        self.assertIn(middle + " kept_open", out)
        self.assertNotIn(early + " kept_open", out)
        self.assertEqual(self.cli("cutover", "support", "--seen-before", "not-a-time")[0], 2)

    def test_dry_run_changes_nothing(self):
        self.add(state="done", received=0, done=500)
        seen = self.add(received=100)
        later = self.add(received=600)
        before = self.records()
        code, out, _ = self.cli("cutover", "support")
        self.assertEqual(code, 0)
        self.assertIn("mode=dry-run", out)
        self.assertIn("closed=1 kept_open=1 redelivered=1", out)
        self.assertIn("dry run: nothing changed", out)
        self.assertEqual(self.state(seen), ("assigned", None))
        self.assertEqual(self.state(later), ("assigned", None))
        self.assertEqual(self.records(), before)

    def test_rerun_does_not_count_its_own_closures_as_lane_activity(self):
        self.add(state="done", received=0, done=500)
        self.add(received=100)
        later = self.add(received=600)
        self.assertEqual(self.cli("cutover", "support", "--apply")[0], 0)
        # Owner reactions, deletions and console closures are not lane activity either.
        self.add(state="done", received=700, done=990, note="owner_reaction:white_check_mark")
        self.add(state="done", received=710, done=995, note="human:console")
        code, out, _ = self.cli("cutover", "support", "--apply")
        self.assertEqual(code, 0)
        self.assertIn(f"lane_last_seen={iso(500)}", out)
        self.assertIn("closed=0 kept_open=1", out)
        self.assertEqual(self.state(later)[0], "assigned")

    def test_context_and_owner_rows_are_refused_and_unknown_lane_is_3(self):
        context = self.add(received=10, note="context", reason="human:cli")
        owner = self.add(received=20, reason="owner_post")
        normal = self.add(received=30)
        code, out, err = self.cli("cutover", "support", "--apply")
        self.assertEqual(code, 2)
        self.assertIn("refused=2", out)
        self.assertEqual(err.splitlines(), [
            f"refused: {context} is a context row, not an inbox item",
            f"refused: {owner} is an owner post, not an inbox item"])
        self.assertEqual(self.state(context)[0], "assigned")
        self.assertIn(normal + " kept_open", out)
        self.assertNotIn(context, [row["id"] for row in self.records() if row["kind"] == "backfill"])
        self.assertEqual(self.cli("cutover", "missing-lane")[0], 3)


if __name__ == "__main__":
    unittest.main()
