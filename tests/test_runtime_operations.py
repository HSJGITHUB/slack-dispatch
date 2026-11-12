"""Offline runtime checks with best-effort cleanup of system temporary fixtures."""

from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from slack_dispatch.runtime import (
    AlreadyRunning, ControlFlags, HeartbeatWriter, SingleInstanceLock,
    configure_logging, is_stale, read_heartbeat,
)
from slack_dispatch.runtime.logging_setup import RedactionFilter

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")


def fixture(test):
    directory = Path(tempfile.mkdtemp(prefix="case-"))
    test.addCleanup(shutil.rmtree, directory, ignore_errors=True)
    return directory


class RuntimeTests(unittest.TestCase):
    def test_lock_child_exclusion_release_and_crash(self):
        path = fixture(self) / "service.lock"
        child = (
            "import sys; from slack_dispatch.runtime import SingleInstanceLock, AlreadyRunning\n"
            "try:\n"
            " with SingleInstanceLock(sys.argv[1]): print('acquired', flush=True)\n"
            "except AlreadyRunning as e: print(e.pid, flush=True); sys.exit(23)\n"
        )
        with SingleInstanceLock(path) as lock:
            with self.assertRaises(AlreadyRunning):
                lock.__enter__()
            result = subprocess.run([sys.executable, "-B", "-c", child, str(path)],
                                    cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 23, (result.stderr or ""))
            self.assertEqual((result.stdout or "").strip(), str(os.getpid()))
        result = subprocess.run([sys.executable, "-B", "-c", child, str(path)],
                                cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, (result.stderr or ""))
        self.assertTrue(path.exists())
        crash = (
            "import os, sys; from slack_dispatch.runtime import SingleInstanceLock; "
            "lock=SingleInstanceLock(sys.argv[1]); lock.__enter__(); os._exit(0)"
        )
        subprocess.run([sys.executable, "-B", "-c", crash, str(path)],
                       cwd=ROOT, check=True, encoding="utf-8", errors="replace", timeout=15)
        with SingleInstanceLock(path):
            pass

    def test_heartbeat_clock_atomic_read_and_stale(self):
        path = fixture(self) / "heartbeat.json"
        now = [1000.0]
        writer = HeartbeatWriter(path, 10, lambda: now[0])
        self.assertIsNone(read_heartbeat(path))
        self.assertTrue(writer.write(version="test", counters={"seen": 2}))
        original = path.read_bytes()
        hb = read_heartbeat(path)
        self.assertEqual(hb["pid"], os.getpid())
        self.assertEqual(hb["counters"], {"seen": 2})
        self.assertEqual(set(hb), {"schema", "ts", "pid", "ok", "status", "version", "counters"})
        self.assertIsNotNone(datetime.fromisoformat(hb["ts"]).tzinfo)
        now[0] = 1009
        self.assertFalse(writer.write())
        self.assertEqual(original, path.read_bytes())
        self.assertFalse(is_stale(hb, 1010, 10))
        self.assertTrue(is_stale(hb, 1011, 10))
        self.assertTrue(is_stale(hb, 999, 10))
        now[0] = 1010
        with patch("slack_dispatch.runtime.heartbeat.os.replace", side_effect=OSError("dummy")):
            with self.assertRaises(OSError):
                writer.write()
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(writer.write(ok=False, status="stopping"))
        self.assertFalse(read_heartbeat(path)["ok"])
        now[0] = 900
        self.assertTrue(writer.write())
        self.assertTrue(writer.write(force=True))
        path.write_text("{broken", encoding="utf-8")
        self.assertIsNone(read_heartbeat(path))
        for bad in (None, {}, {"schema": "other"}, {"schema": hb["schema"], "ts": "bad"}):
            self.assertTrue(is_stale(bad, 1000, 10))
        with self.assertRaises(ValueError):
            HeartbeatWriter(path, 0, lambda: 0)

    def test_control_flags_preserve_archived_stops(self):
        directory = fixture(self)
        flags = ControlFlags(directory)
        self.assertFalse(flags.should_stop())
        self.assertTrue(flags.wake_enabled())
        self.assertIsNone(flags.acknowledge_stop())
        (directory / "wake.off").touch()
        self.assertFalse(flags.wake_enabled())
        archived = []
        for content in ("first", "second"):
            (directory / "stop.flag").write_text(content, encoding="utf-8")
            self.assertTrue(flags.should_stop())
            target = flags.acknowledge_stop()
            self.assertEqual(target.parent, directory / "done")
            self.assertTrue(target.name.endswith("_stop.flag"))
            self.assertEqual(target.read_text(), content)
            self.assertFalse(flags.should_stop())
            archived.append(target)
        self.assertNotEqual(*archived)
        self.assertEqual(len(list((directory / "done").iterdir())), 2)

    def test_logging_redacts_tokens_attributes_exceptions_and_rotates(self):
        directory = fixture(self)
        logger = configure_logging(directory, "DEBUG")
        configure_logging(directory, "DEBUG")
        handlers = [h for h in logger.handlers if getattr(h, "_dispatch_runtime", False)]
        self.assertEqual(len(handlers), 1)
        handler = handlers[0]
        try:
            tokens = [prefix + "dummy-123" for prefix in ("xoxb-", "xoxa-", "xoxp-", "xapp-")]
            logger.info("tokens %s", tokens, extra={"text": "private-text", "body": "private-body"})
            logger.info({"body": "private-nested", "status": tokens[0]})
            logger.error("dummy failure", exc_info=(RuntimeError, RuntimeError(tokens[-1]), None))
            handler.flush()
            text = (directory / "service.log").read_text(encoding="utf-8")
            for token in tokens:
                self.assertNotIn(token, text)
            for private in ("private-text", "private-body", "private-nested"):
                self.assertNotIn(private, text)
            self.assertIn("***", text)
            record = logging.makeLogRecord({"msg": "safe", "text": "private", "body": "private"})
            RedactionFilter().filter(record)
            self.assertNotIn("text", record.__dict__)
            self.assertNotIn("body", record.__dict__)
            self.assertFalse(handler.utc)
            self.assertEqual(handler.when, "MIDNIGHT")
            handler.doRollover()
            logger.info("after rotation")
            handler.flush()
            self.assertTrue(list(directory.glob("service.log.*")))
            self.assertIn("after rotation", (directory / "service.log").read_text())
        finally:
            logger.removeHandler(handler)
            handler.close()


@unittest.skipUnless(POWERSHELL, "Windows PowerShell is unavailable")
class WindowsToolTests(unittest.TestCase):
    def run_ps(self, script, *args, expected=0):
        result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                                 "Bypass", "-File", str(script), *map(str, args)],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
        self.assertEqual(result.returncode, expected, (result.stdout or "") + (result.stderr or ""))
        return result.stdout or ""

    def test_real_template_render_and_whatif(self):
        directory = fixture(self)
        script = ROOT / "deploy/windows/install_task.ps1"
        args = ("-Python", r"C:\demo & test\pythonw.exe", "-Workdir", r"C:\demo & test\release",
                "-Config", r"C:\demo & test\state\config.yaml", "-User", r"DEMO\operator",
                "-OutputDirectory", directory)
        preview = self.run_ps(script, *args, "-WhatIf")
        self.assertIn("schtasks.exe /Create", preview)
        self.assertEqual(list(directory.iterdir()), [])
        output = self.run_ps(script, *args)
        xml_files = list(directory.glob("*.xml"))
        self.assertEqual(len(xml_files), 1)
        self.assertIn(str(xml_files[0]), output)
        rendered = xml_files[0].read_text(encoding="utf-8")
        for name in ("python", "workdir", "config", "user"):
            self.assertNotIn("{" + name + "}", rendered)
        root = ET.fromstring(rendered)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.find(".//t:Command", ns).text, args[1])
        self.assertEqual(root.find(".//t:Interval", ns).text, "PT1M")
        self.assertEqual(root.find(".//t:MultipleInstancesPolicy", ns).text, "IgnoreNew")
        self.assertEqual(root.find(".//t:ExecutionTimeLimit", ns).text, "PT0S")
        self.assertIn('-X utf8 -B -m slack_dispatch.service --config "',
                      root.find(".//t:Arguments", ns).text)

    @unittest.skipUnless(shutil.which("git"), "git is unavailable")
    def test_tagged_deploy_manifest_and_whatif(self):
        directory = fixture(self)
        checkout = directory / "checkout"
        checkout.mkdir()

        git_env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_AUTHOR_NAME="Dummy", GIT_AUTHOR_EMAIL="dummy@example.invalid",
                       GIT_COMMITTER_NAME="Dummy", GIT_COMMITTER_EMAIL="dummy@example.invalid")

        def git(*args):
            result = subprocess.run(["git", "-C", str(checkout), *args], check=True,
                                  text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=15, env=git_env)
            return (result.stdout or "").strip()

        git("init", "--template=")
        (checkout / "sample.txt").write_text("tagged\n", encoding="utf-8")
        git("add", "sample.txt")
        git("-c", "user.name=Dummy", "-c", "user.email=dummy@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-m", "Dummy fixture")
        git("tag", "v-test")
        commit = git("rev-parse", "HEAD")
        (checkout / "sample.txt").write_text("dirty\n", encoding="utf-8")
        target = directory / "release"
        archive = directory / "archives"
        script = ROOT / "deploy/deploy.ps1"
        args = ("-Checkout", checkout, "-Tag", "v-test", "-Target", target,
                "-ArchiveDirectory", archive)
        self.run_ps(script, *args, "-WhatIf")
        self.assertFalse(target.exists())
        self.assertFalse(archive.exists())
        output = self.run_ps(script, *args)
        manifest = json.loads((target / "DEPLOYED.json").read_text(encoding="utf-8"))
        self.assertEqual(json.loads(output), manifest)
        self.assertEqual(manifest["tag"], "v-test")
        self.assertEqual(manifest["commit"], commit)
        self.assertEqual((target / "sample.txt").read_text(), "tagged\n")
        self.assertEqual(manifest["sha256"], {
            "sample.txt": hashlib.sha256((target / "sample.txt").read_bytes()).hexdigest()})
        self.run_ps(script, *args, expected=1)

    def test_restart_waits_for_original_process_and_whatif(self):
        directory = fixture(self)
        code = (
            "import pathlib, sys, time; from slack_dispatch.runtime import HeartbeatWriter, ControlFlags\n"
            "p=pathlib.Path(sys.argv[1]); HeartbeatWriter(p/'heartbeat.json').write(); "
            "print('ready', flush=True)\n"
            "flags=ControlFlags(p)\n"
            "while not flags.should_stop(): time.sleep(.05)\n"
            "flags.acknowledge_stop()\n"
        )
        child = subprocess.Popen([sys.executable, "-B", "-c", code, str(directory)],
                                 cwd=ROOT, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            import time
            deadline = time.monotonic() + 10
            while read_heartbeat(directory / "heartbeat.json") is None:
                if child.poll() is not None or time.monotonic() > deadline:
                    self.fail("Dummy heartbeat process failed to become ready")
                time.sleep(.05)
            script = ROOT / "deploy/windows/restart.ps1"
            args = ("-StateDirectory", directory, "-TimeoutSeconds", "10")
            self.run_ps(script, *args, "-WhatIf")
            self.assertFalse((directory / "stop.flag").exists())
            self.assertIsNone(child.poll())
            output = self.run_ps(script, *args)
            self.assertIn("Previous process exited", output)
            self.assertEqual(child.wait(timeout=10), 0)
            self.assertEqual(len(list((directory / "done").glob("*_stop.flag"))), 1)
            self.run_ps(script, *args, expected=1)
        finally:
            try:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            finally:
                child.communicate()


if __name__ == "__main__":
    unittest.main()
