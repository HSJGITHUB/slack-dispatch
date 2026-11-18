import builtins
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from slack_dispatch.inbox.files import append, read, record
from tests.fakes.fake_store import FakeStore


def writer(root, prefix):
    for number in range(20):
        append(root, "support", [record({"id": f"slack:CDUMMY:{prefix}{number}", "channel": "CDUMMY", "ts": str(number), "text": "PRIVATE_BODY"})])


class InboxTests(unittest.TestCase):
    def test_two_process_writers(self):
        with tempfile.TemporaryDirectory() as root:
            context = multiprocessing.get_context("spawn")
            processes = [context.Process(target=writer, args=(root, str(i))) for i in range(2)]
            try:
                for process in processes:
                    process.start()
                for process in processes:
                    process.join(30)
            finally:
                # P33: never leave a writer running. A live child keeps inbox files open, and
                # the temporary directory cleanup then fails (WinError 145) and hides the cause.
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        process.join(5)
            self.assertEqual([process.exitcode for process in processes], [0, 0])
            rows = read(root, "support")
            self.assertEqual(len(rows), 40)
            self.assertEqual(len({r["id"] for r in rows}), 40)
            self.assertNotIn("PRIVATE_BODY", (Path(root) / "support.jsonl").read_text())
            self.assertTrue((Path(root) / "support.lock").exists())
            # The lock file format is unchanged: one marker byte on Windows, empty on POSIX.
            self.assertEqual((Path(root) / "support.lock").read_bytes(),
                             b"0" if os.name == "nt" else b"")

    def test_join_done_and_reroute(self):
        store = FakeStore()
        message = {"id": "slack:CDUMMY:1", "channel": "CDUMMY", "ts": "1", "state": "done", "lane": "support", "text": "PRIVATE_BODY"}
        store.messages[message["id"]] = message
        with tempfile.TemporaryDirectory() as root:
            append(root, "support", [dict(record(message), body="PRIVATE_BODY")])
            self.assertEqual(read(root, "support", store=store), [])
            rows = read(root, "support", store=store, include_done=True)
            self.assertEqual(rows[0]["state"], "done")
            self.assertNotIn("text", rows[0])
            store.set_route(message["id"], "other", "test")
            self.assertEqual(read(root, "support", store=store), [])
            with self.assertRaises(ValueError):
                append(root, "../escape", [])
            self.assertEqual(read(root, "empty"), [])

    def test_atomic_create_fallback_releases_lock(self):
        original_import = builtins.__import__

        def fallback(name, *args, **kwargs):
            if name in ("msvcrt", "fcntl"):
                raise ImportError("Lock module unavailable")
            return original_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as root:
            with patch("builtins.__import__", side_effect=fallback):
                writer(root, "fallback")
                self.assertEqual(len(read(root, "support")), 20)
            self.assertFalse((Path(root) / "support.lock").exists())
            self.assertTrue((Path(root) / "support.lock.released").exists())

    def test_read_without_lock_has_no_filesystem_side_effects(self):
        import json

        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "missing"
            with patch("slack_dispatch.inbox.files.lane_lock") as lock:
                self.assertEqual(read(missing, "support"), [])
                self.assertFalse(missing.exists())
                row = record({"id": "slack:CDUMMY:1", "channel": "CDUMMY", "ts": "1"})
                path = Path(root) / "support.jsonl"
                path.write_text(json.dumps(row) + "\n", encoding="utf-8")
                self.assertEqual(read(root, "support"), [row])
                self.assertEqual(list(Path(root).iterdir()), [path])
                lock.assert_not_called()
            with self.assertRaises(ValueError):
                read(root, "../escape")
