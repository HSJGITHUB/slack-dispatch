"""P33: inbox appends and reads wait for the lane lock instead of failing on Windows."""
import errno
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from slack_dispatch.inbox import files
from slack_dispatch.inbox.files import append, read, record

WINDOWS = os.name == "nt"


def dummy_record(name, number):
    return record({"id": f"slack:CDUMMY:{name}-{number}", "channel": "CDUMMY", "ts": str(number)})


def hold_raw_lock(path, ready, release):
    """Hold byte 0 of an existing lock file without writing to it.

    This is the state another writer is in between taking the lock and writing the marker
    byte. Before P33 a second writer wrote the marker without the lock and failed here.
    """
    import msvcrt

    with open(path, "a+b") as handle:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            ready.set()
            release.wait(30)
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def fresh_lane_writer(root, name, lanes, go):
    """Race the other writers on the first append of many fresh lanes."""
    go.wait(30)
    for number in range(lanes):
        append(root, f"lane{number}", [dummy_record(name, number)])


def shared_lane_writer(root, name, count, go):
    go.wait(30)
    for number in range(count):
        append(root, "support", [dummy_record(name, number)])


def looping_reader(root, go, stop):
    go.wait(30)
    while not stop.is_set():
        for row in read(root, "support"):
            if set(row) != {"id", "delivered_at", "channel", "ts", "thread_ts", "followup", "kind"}:
                raise AssertionError("Unexpected inbox row shape")


def finish(processes, timeout=60):
    """Join every child within one deadline and terminate stragglers; return exit codes."""
    deadline = time.monotonic() + timeout
    try:
        for process in processes:
            process.join(max(0.0, deadline - time.monotonic()))
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
    return [process.exitcode for process in processes]


class RetryTransientTests(unittest.TestCase):
    @staticmethod
    def sharing_error(winerror):
        err = PermissionError(errno.EACCES, "Permission denied")
        err.winerror = winerror
        return err

    def test_retries_sharing_and_lock_violations_then_succeeds(self):
        calls = []

        def action():
            calls.append(1)
            if len(calls) < 3:
                raise self.sharing_error(32 if len(calls) == 1 else 33)
            return "opened"

        with patch.object(files.time, "sleep") as sleep:
            self.assertEqual(files._retry_transient(action, "support.jsonl"), "opened")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(all(call.args[0] > 0 for call in sleep.call_args_list))

    def test_unrelated_errors_propagate_without_retry(self):
        for err in (self.sharing_error(5), PermissionError(errno.EACCES, "Permission denied"),
                    FileNotFoundError(errno.ENOENT, "missing"), OSError(errno.EIO, "io")):
            calls = []

            def action(err=err):
                calls.append(1)
                raise err

            with self.subTest(err=repr(err)), patch.object(files.time, "sleep") as sleep:
                with self.assertRaises(type(err)) as caught:
                    files._retry_transient(action, "support.jsonl")
                self.assertIs(caught.exception, err)
                self.assertEqual(len(calls), 1)
                sleep.assert_not_called()

    def test_gives_up_with_clear_error_after_timeout(self):
        err = self.sharing_error(32)

        def action():
            raise err

        started = time.monotonic()
        with self.assertRaises(TimeoutError) as caught:
            files._retry_transient(action, "support.jsonl", timeout=0.1)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("Inbox file busy: support.jsonl", str(caught.exception))
        self.assertIs(caught.exception.__cause__, err)

    def test_append_and_read_retry_a_transient_open_failure(self):
        real_open = Path.open
        failures = {"append": 1}

        def flaky_open(self, *args, **kwargs):
            if self.suffix == ".jsonl" and failures["append"]:
                failures["append"] -= 1
                raise RetryTransientTests.sharing_error(32)
            return real_open(self, *args, **kwargs)

        with tempfile.TemporaryDirectory() as root:
            with patch.object(Path, "open", flaky_open):
                append(root, "support", [dummy_record("w", 1)])
            self.assertEqual(failures["append"], 0)
            real_read_text = Path.read_text
            failures["read"] = 2

            def flaky_read_text(self, *args, **kwargs):
                if self.suffix == ".jsonl" and failures["read"]:
                    failures["read"] -= 1
                    raise RetryTransientTests.sharing_error(33)
                return real_read_text(self, *args, **kwargs)

            with patch.object(Path, "read_text", flaky_read_text):
                self.assertEqual([row["id"] for row in read(root, "support")], ["slack:CDUMMY:w-1"])
            self.assertEqual(failures["read"], 0)


class UnlockedReadTests(unittest.TestCase):
    def test_read_retries_under_lock_when_a_first_writer_appears(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "support.jsonl"
            path.write_text('{"id": "slack:CDUMMY:1", "chan', encoding="utf-8")
            real = files._read_rows
            calls = []

            def first_writer_arrives(root_arg, lane, locked):
                calls.append(locked)
                if not locked:
                    # Simulate the writer: it creates the lock and finishes its append while
                    # this unlocked read is parsing a partial line.
                    try:
                        return real(root_arg, lane, locked)
                    finally:
                        path.write_text(
                            '{"id": "slack:CDUMMY:1", "delivered_at": "x", "channel": "CDUMMY", '
                            '"ts": "1", "thread_ts": null, "followup": false, "kind": "new"}\n',
                            encoding="utf-8")
                        (Path(root) / "support.lock").write_bytes(b"0" if WINDOWS else b"")
                return real(root_arg, lane, locked)

            with patch.object(files, "_read_rows", first_writer_arrives):
                rows = read(root, "support")
            self.assertEqual(calls, [False, True])
            self.assertEqual([row["id"] for row in rows], ["slack:CDUMMY:1"])

    def test_corrupt_inbox_without_lock_still_raises(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "support.jsonl").write_text("{not json}\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                read(root, "support")
            self.assertFalse((Path(root) / "support.lock").exists())


@unittest.skipUnless(WINDOWS, "msvcrt byte-range locking is Windows only")
class WindowsHeldLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.lock = Path(self.root) / "support.lock"
        self.lock.write_bytes(b"")
        context = multiprocessing.get_context("spawn")
        self.ready, self.release = context.Event(), context.Event()
        self.holder = context.Process(target=hold_raw_lock,
                                      args=(str(self.lock), self.ready, self.release))
        self.holder.start()
        self.addCleanup(finish, [self.holder], 30)
        self.addCleanup(self.release.set)
        self.assertTrue(self.ready.wait(30), "lock holder did not start")

    def run_while_held(self, action, hold=0.3):
        outcome = {}

        def target():
            try:
                outcome["value"] = action()
            except BaseException as err:  # noqa: BLE001 (reported by the assertion below)
                outcome["error"] = err

        thread = threading.Thread(target=target)
        thread.start()
        thread.join(hold)
        self.assertNotIn("error", outcome, "must wait for the held lock, not fail")
        self.assertTrue(thread.is_alive(), "must wait while another process holds the lock")
        self.release.set()
        thread.join(30)
        self.assertEqual(finish([self.holder], 30), [0])
        self.assertNotIn("error", outcome)
        return outcome.get("value")

    def test_append_waits_for_a_holder_of_an_empty_lock_file(self):
        self.run_while_held(lambda: append(self.root, "support", [dummy_record("w", 1)]))
        self.assertEqual([row["id"] for row in read(self.root, "support")], ["slack:CDUMMY:w-1"])
        self.assertEqual(self.lock.read_bytes(), b"0")

    def test_read_waits_for_a_holder_of_an_empty_lock_file(self):
        (Path(self.root) / "support.jsonl").write_text("", encoding="utf-8")
        self.assertEqual(self.run_while_held(lambda: read(self.root, "support")), [])
        self.assertEqual(self.lock.read_bytes(), b"0")

    def test_lock_timeout_is_a_clear_error(self):
        with patch.object(files, "LOCK_TIMEOUT", 0.2):
            started = time.monotonic()
            with self.assertRaises(TimeoutError) as caught:
                append(self.root, "support", [dummy_record("w", 1)])
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(str(caught.exception), "Inbox lock unavailable")
        self.assertFalse((Path(self.root) / "support.jsonl").exists())
        # The waiter never wrote: the file is still empty (its locked byte cannot be read).
        self.assertEqual(self.lock.stat().st_size, 0)


class MultiProcessContentionTests(unittest.TestCase):
    def test_writers_racing_on_fresh_lock_files(self):
        processes_count, lanes = 4, 40
        with tempfile.TemporaryDirectory() as root:
            context = multiprocessing.get_context("spawn")
            go = context.Event()
            processes = [context.Process(target=fresh_lane_writer, args=(root, f"w{n}", lanes, go))
                         for n in range(processes_count)]
            try:
                for process in processes:
                    process.start()
                go.set()
            finally:
                codes = finish(processes)
            self.assertEqual(codes, [0] * processes_count)
            for number in range(lanes):
                rows = read(root, f"lane{number}")
                self.assertEqual(len({row["id"] for row in rows}), processes_count)

    def test_writers_and_readers_on_one_lane(self):
        writers, count = 6, 15
        with tempfile.TemporaryDirectory() as root:
            context = multiprocessing.get_context("spawn")
            go, stop = context.Event(), context.Event()
            writer_processes = [
                context.Process(target=shared_lane_writer, args=(root, f"w{n}", count, go))
                for n in range(writers)]
            reader_processes = [context.Process(target=looping_reader, args=(root, go, stop))
                                for _ in range(2)]
            try:
                for process in writer_processes + reader_processes:
                    process.start()
                go.set()
                writer_codes = finish(writer_processes)
            finally:
                stop.set()
                reader_codes = finish(reader_processes, 30)
                finish(writer_processes, 5)
            self.assertEqual(writer_codes, [0] * writers)
            self.assertEqual(reader_codes, [0, 0])
            rows = read(root, "support")
            self.assertEqual(len(rows), writers * count)
            self.assertEqual(len({row["id"] for row in rows}), writers * count)


if __name__ == "__main__":
    unittest.main()
