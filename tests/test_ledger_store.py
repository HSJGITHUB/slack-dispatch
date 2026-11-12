import unittest

from slack_dispatch.ledger.store import LedgerStore


STAMP = "2026-01-01T00:00:00+00:00"


def event(**changes):
    value = dict(source="slack", event_id="Ev0001", event_type="message", subtype=None,
                 channel="C0000000001", channel_type="channel", user="U0000000001",
                 ts="100.000001", thread_ts=None, is_thread_reply=False, has_files=False,
                 received_at=STAMP, text="synthetic body")
    value.update(changes)
    return value


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.store = LedgerStore(":memory:")
        self.store.open()
        self.store.migrate()
        self.addCleanup(self.store.close)

    def test_migrate_twice_and_foreign_keys(self):
        self.store.migrate()
        self.assertEqual(self.store._rows("SELECT version FROM schema_version"), [{"version": 1}])
        self.assertEqual(self.store._rows("PRAGMA foreign_keys")[0]["foreign_keys"], 1)
        self.assertIn("body", {r["name"] for r in self.store._rows("PRAGMA table_info(messages)")})

    def test_upgrade_scaffold_and_rollback_future_version(self):
        # Simulate a database with the original messages columns and no version row.
        for column in ("event_id", "channel_type", "has_files", "edited_at", "note", "suggestion_reason"):
            self.store._db.execute(f"ALTER TABLE messages DROP COLUMN {column}")
        self.store._db.execute("DELETE FROM schema_version")
        self.store.migrate()
        result = self.store.record_event(event())
        self.assertTrue(result.created)
        self.store._db.execute("UPDATE schema_version SET version=2")
        with self.assertRaisesRegex(RuntimeError, "Unsupported"):
            self.store.migrate()
        self.assertFalse(self.store._db.in_transaction)

    def test_dedup_and_body_default(self):
        result = self.store.record_event(event())
        self.assertTrue(result.created)
        self.assertFalse(result.duplicate)
        self.assertTrue(self.store.record_event(event()).duplicate)
        self.assertTrue(self.store.record_event(event(event_id="Ev0002")).duplicate)
        self.assertEqual(len(self.store.list_messages()), 1)
        self.assertNotIn("body", self.store.get_message(result.message_id))
        self.assertIsNone(self.store.get_message(result.message_id, include_body=True)["body"])
        self.assertNotIn("body", self.store.list_messages()[0])
        self.assertIsNone(self.store.get_message("missing"))

    def test_edits_delete_and_body_opt_in(self):
        self.store.store_body = True
        mid = self.store.record_event(event()).message_id
        edit = event(event_id="Ev0002", subtype="message_changed", text="synthetic edit")
        self.assertFalse(self.store.record_event(edit).created)
        row = self.store.get_message(mid, include_body=True)
        self.assertEqual(row["body"], "synthetic edit")
        self.assertEqual(row["edited_at"], STAMP)
        self.assertEqual(row["followup"], 1)
        self.assertFalse(self.store.record_event(event(event_id="Ev0003", ts="200.0",
                                                      subtype="message_changed")).created)
        self.assertEqual(len(self.store.list_messages()), 1)
        self.store.record_event(event(event_id="Ev0004", subtype="message_deleted"))
        self.assertIsNone(self.store.get_message(mid, include_body=True)["body"])
        self.assertEqual(self.store.get_message(mid)["note"], "message_deleted")

    def test_reactions_files_and_channel_metadata(self):
        mid = self.store.record_event(event()).message_id
        added = event(event_id="Ev0002", event_type="reaction_added", reaction="eyes")
        result = self.store.record_event(added)
        self.assertEqual(result.message_id, mid)
        self.assertFalse(result.created)
        self.assertEqual(self.store.list_reactions(mid)[0]["added_at"], STAMP)
        self.store.record_event(event(event_id="Ev0003", event_type="reaction_removed", reaction="eyes"))
        self.assertEqual(self.store.list_reactions(mid)[0]["removed_at"], STAMP)
        self.store.record_event(event(event_id="Ev0004", event_type="file_shared"))
        self.assertEqual(self.store.get_message(mid)["has_files"], 1)
        for index, kind in enumerate(("channel_created", "member_joined_channel"), 5):
            result = self.store.record_event(event(event_id=f"Ev000{index}", event_type=kind,
                                                   channel="C0000000002", ts=None))
            self.assertEqual(result.kind, "channel_meta")
            self.assertIsNone(result.message_id)
        self.assertEqual(self.store.list_channels(), ["C0000000001", "C0000000002"])
        # A reaction can arrive before its referenced message.
        self.store.record_event(event(event_id="Ev0007", event_type="reaction_added", ts="300.0", reaction="eyes"))
        self.assertEqual(len(self.store.list_messages()), 1)

    def test_routes_lanes_bindings_states_and_filters(self):
        mid = self.store.record_event(event()).message_id
        self.store.upsert_lane("support", session_pointer="dummy-session", paused=True)
        self.store.upsert_lane("support", paused=False)
        self.assertEqual(self.store.get_lane("support")["session_pointer"], "dummy-session")
        self.assertEqual(self.store.get_lane("support")["paused"], 0)
        self.assertEqual(len(self.store.list_lanes()), 1)
        self.store.bind_thread("slack", "C0000000001", "100.000001", "support")
        self.assertEqual(self.store.lookup_binding("slack", "C0000000001", "100.000001"), "support")
        self.assertIsNone(self.store.lookup_binding("slack", "C0000000001", "999.0"))
        self.store.set_suggestion(mid, "support", "synthetic reason", "test-v1")
        self.assertIsNone(self.store.get_message(mid)["lane"])
        self.store.set_route(mid, "support", "channel default")
        self.assertIsNotNone(self.store.get_message(mid)["assigned_at"])
        self.store.mark_followup(mid)
        for state in ("acked", "done", "human_pending", "excluded", "new"):
            self.store.set_state(mid, state, note="synthetic note")
            self.assertEqual(self.store.get_message(mid)["state"], state)
        self.assertIsNotNone(self.store.get_message(mid)["ack_at"])
        self.assertIsNotNone(self.store.get_message(mid)["done_at"])
        self.assertEqual(len(self.store.list_messages(state="new", lane="support", since=STAMP)), 1)
        self.assertEqual(self.store.list_messages(state="done"), [])
        self.assertEqual(self.store.list_messages(since="2027", limit=1), [])
        self.assertEqual(self.store.list_messages(limit=0), [])
        with self.assertRaises(ValueError):
            self.store.set_state(mid, "invalid")
        with self.assertRaises(ValueError):
            self.store.list_messages(limit=-1)

    def test_cursor_and_transaction_rollback(self):
        self.assertIsNone(self.store.get_cursor("slack", "C0000000001"))
        self.store.set_cursor("slack", "C0000000001", "100.0")
        self.store.set_cursor("slack", "C0000000001", "200.0")
        self.assertEqual(self.store.get_cursor("slack", "C0000000001"), "200.0")
        with self.assertRaises(Exception):
            self.store.record_event(event(received_at=None))
        self.assertEqual(self.store.list_messages(), [])
        self.assertFalse(self.store._db.in_transaction)
        statements = []
        self.store._db.set_trace_callback(statements.append)
        self.store.upsert_lane("support")
        self.assertIn("BEGIN IMMEDIATE", statements)
        self.assertIn("COMMIT", statements)
