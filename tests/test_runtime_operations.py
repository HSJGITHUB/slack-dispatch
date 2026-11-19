"""Offline runtime checks with best-effort cleanup of system temporary fixtures."""

from datetime import datetime, timedelta, timezone
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
import warnings
import xml.etree.ElementTree as ET
import zipfile

from slack_dispatch.runtime import (
    AlreadyRunning, ControlFlags, HeartbeatWriter, SingleInstanceLock,
    configure_logging, is_stale, read_heartbeat,
)
from slack_dispatch.runtime.logging_setup import RedactionFilter

ROOT = Path(__file__).resolve().parents[1]
# Every Windows tool test runs once per installed PowerShell: Windows PowerShell 5.1 and
# PowerShell 7. pwsh.exe is looked up by that name so the variant only exists on Windows.
WINDOWS_POWERSHELL = shutil.which("powershell.exe")
POWERSHELL_7 = shutil.which("pwsh.exe")
# PowerShell 7 colours error output with ANSI codes even when it is redirected; NO_COLOR
# turns that off so output comparisons see plain text. Windows PowerShell 5.1 ignores it.
PLAIN_TEXT_ENV = {"NO_COLOR": "1"}
DEPLOY_EXTRACT_DRIVER = """param([string]$Script, [string]$Archive, [string]$Destination)
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -ge 7) { $PSStyle.OutputRendering = 'PlainText' }
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Script, [ref]$null, [ref]$null)
foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst]) {
        . ([scriptblock]::Create($statement.Extent.Text))
    }
}
Expand-DeploymentArchive -ArchivePath $Archive -DestinationPath $Destination
Write-Output 'extracted'
"""
FAILING_ARCHIVE_MODULE = (
    "function Expand-Archive { throw 'Cannot find an overload for \"ExtractToDirectory\" "
    "and the argument count: \"3\".' }\n"
    "Export-ModuleMember -Function Expand-Archive\n"
)


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


class DeployScriptTests(unittest.TestCase):
    def test_deploy_script_does_not_depend_on_archive_module(self):
        code = [line.split("#", 1)[0] for line in
                (ROOT / "deploy/deploy.ps1").read_text(encoding="utf-8").splitlines()]
        for forbidden in ("Expand-Archive", "ExtractToDirectory"):
            self.assertFalse([line for line in code if forbidden in line], forbidden)
        text = "\n".join(code)
        for required in ("Add-Type -AssemblyName System.IO.Compression\n",
                         "Add-Type -AssemblyName System.IO.Compression.FileSystem\n",
                         "[System.IO.Compression.ZipFile]::OpenRead(",
                         "[System.IO.Compression.ZipFileExtensions]::ExtractToFile($item.Entry, $item.Path, $false)",
                         "$zip.Dispose()"):
            self.assertIn(required, text)


class WindowsToolChecks:
    """Windows tool tests. The subclasses below run all of them under each installed PowerShell."""

    powershell = None
    minimum_major = None
    maximum_major = None

    def run_ps_result(self, script, *args, expected=0, env=None):
        child_env = os.environ.copy()
        child_env.update(PLAIN_TEXT_ENV)
        if env is not None:
            child_env.update(env)
        result = subprocess.run([self.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                                 "Bypass", "-File", str(script), *map(str, args)],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
                                env=child_env)
        self.assertEqual(result.returncode, expected, (result.stdout or "") + (result.stderr or ""))
        return result

    def run_ps(self, script, *args, expected=0, env=None):
        return self.run_ps_result(script, *args, expected=expected, env=env).stdout or ""

    def test_engine_is_the_intended_powershell(self):
        script = fixture(self) / "version.ps1"
        script.write_text("Write-Output $PSVersionTable.PSVersion.Major\n", encoding="utf-8")
        major = int(self.run_ps(script).strip())
        self.assertGreaterEqual(major, self.minimum_major)
        if self.maximum_major is not None:
            self.assertLessEqual(major, self.maximum_major)

    def test_real_template_render_and_whatif(self):
        directory = fixture(self)
        script = ROOT / "deploy/windows/install_task.ps1"
        args = ("-Python", r"C:\demo & test\pythonw.exe", "-Workdir", r"C:\demo & test\release",
                "-Config", r"C:\demo & test\state\config.yaml", "-User", r"DEMO\operator",
                "-OutputDirectory", directory)
        preview = self.run_ps(script, *args, "-WhatIf")
        self.assertIn("schtasks.exe /Create", preview)
        self.assertEqual(list(directory.iterdir()), [])
        before_render = datetime.now()
        output = self.run_ps(script, *args)
        after_render = datetime.now()
        xml_files = list(directory.glob("*.xml"))
        self.assertEqual(len(xml_files), 1)
        self.assertIn(str(xml_files[0]), output)
        self.assertTrue(xml_files[0].read_bytes().startswith(b"\xff\xfe"))
        rendered = xml_files[0].read_text(encoding="utf-16")
        self.assertEqual(rendered.splitlines()[0], '<?xml version="1.0" encoding="UTF-16"?>')
        for name in ("python", "workdir", "config", "user", "start"):
            self.assertNotIn("{" + name + "}", rendered)
        root = ET.fromstring(rendered)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.find(".//t:Command", ns).text, args[1])
        triggers = root.find("t:Triggers", ns)
        self.assertEqual(len(triggers), 2)
        logon_trigger = triggers.find("t:LogonTrigger", ns)
        time_trigger = triggers.find("t:TimeTrigger", ns)
        self.assertIsNotNone(logon_trigger)
        self.assertIsNotNone(time_trigger)
        for trigger in (logon_trigger, time_trigger):
            self.assertEqual(trigger.find("t:Repetition/t:Interval", ns).text, "PT1M")
            self.assertEqual(trigger.find("t:Repetition/t:StopAtDurationEnd", ns).text, "false")
            self.assertIsNone(trigger.find("t:Repetition/t:Duration", ns))
            self.assertEqual(trigger.find("t:Enabled", ns).text, "true")
        self.assertEqual(logon_trigger.find("t:UserId", ns).text, args[7])
        start_text = time_trigger.find("t:StartBoundary", ns).text
        self.assertRegex(start_text, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
        start = datetime.fromisoformat(start_text)
        self.assertIsNone(start.tzinfo)
        self.assertGreaterEqual(start, before_render - timedelta(minutes=5, seconds=1))
        self.assertLessEqual(start, after_render - timedelta(minutes=5))
        self.assertEqual(root.find(".//t:MultipleInstancesPolicy", ns).text, "IgnoreNew")
        self.assertEqual(root.find(".//t:ExecutionTimeLimit", ns).text, "PT0S")
        self.assertIn('-X utf8 -B -m slack_dispatch.service --config "',
                      root.find(".//t:Arguments", ns).text)

    @unittest.skipUnless(shutil.which("git"), "git is unavailable")
    def test_tagged_deploy_manifest_and_whatif(self):
        self.check_tagged_deploy()

    @unittest.skipUnless(os.name == "nt" and shutil.which("git"), "Windows and git are required")
    def test_tagged_deploy_manifest_with_different_casing(self):
        self.check_tagged_deploy("case")

    @unittest.skipUnless(os.name == "nt" and shutil.which("git"), "Windows and git are required")
    def test_tagged_deploy_manifest_with_short_path(self):
        self.check_tagged_deploy("short")

    @unittest.skipUnless(shutil.which("git"), "git is unavailable")
    def test_tagged_deploy_does_not_use_archive_module(self):
        modules = fixture(self) / "modules"
        module = modules / "Microsoft.PowerShell.Archive"
        module.mkdir(parents=True)
        (module / "Microsoft.PowerShell.Archive.psm1").write_text(FAILING_ARCHIVE_MODULE, encoding="utf-8")
        self.check_tagged_deploy(env={"PSModulePath": str(modules)})

    def extract_with_deploy_function(self, archive, destination, expected=0):
        driver = fixture(self) / "extract_driver.ps1"
        driver.write_text(DEPLOY_EXTRACT_DRIVER, encoding="utf-8")
        result = subprocess.run([self.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                 "-File", str(driver), "-Script", str(ROOT / "deploy/deploy.ps1"),
                                 "-Archive", str(archive), "-Destination", str(destination)],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=45, env=dict(os.environ, **PLAIN_TEXT_ENV))
        output = (result.stdout or "") + (result.stderr or "")
        self.assertEqual(result.returncode, expected, output)
        return output

    def test_deploy_extraction_writes_entries_without_overwriting(self):
        directory = fixture(self)
        archive = directory / "release.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("nested/", b"")
            zf.writestr("nested/binary.bin", bytes(range(256)))
            zf.writestr("empty.bin", b"")
            zf.writestr("sample.txt", b"tagged\n")
        destination = directory / "parent" / "release"
        self.assertIn("extracted", self.extract_with_deploy_function(archive, destination))
        self.assertEqual((destination / "nested" / "binary.bin").read_bytes(), bytes(range(256)))
        self.assertEqual((destination / "empty.bin").read_bytes(), b"")
        self.assertEqual((destination / "sample.txt").read_bytes(), b"tagged\n")
        duplicate = directory / "duplicate.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with zipfile.ZipFile(duplicate, "w") as zf:
                zf.writestr("same.txt", b"first")
                zf.writestr("same.txt", b"second")
                zf.writestr("late.txt", b"late")
        second = directory / "second"
        self.extract_with_deploy_function(duplicate, second, expected=1)
        self.assertEqual((second / "same.txt").read_bytes(), b"first")
        self.assertFalse((second / "late.txt").exists())

    def test_deploy_extraction_refuses_entries_outside_target(self):
        outside = "Archive entry is outside the target directory"
        rooted = "Archive entry is absolute, drive-qualified or empty"
        crafted = ((r"..\evil.txt", outside), ("../evil.txt", outside), ("nested/../../evil.txt", outside),
                   (r"nested\..\..\evil.txt", outside), ("../release2/evil.txt", outside),
                   ("/evil.txt", rooted), (r"\evil.txt", rooted), ("C:/evil.txt", rooted),
                   ("C:evil.txt", rooted), (r"\\server\share\evil.txt", rooted), ("evil.txt:stream", rooted),
                   ("nested/evil|name.txt", "Archive entry is not a valid path"))
        unreadable = "Archive entries cannot be read: "
        for name, message in crafted:
            with self.subTest(name=name):
                directory = fixture(self)
                archive = directory / "crafted.zip"
                with zipfile.ZipFile(archive, "w") as zf:
                    zf.writestr("good.txt", b"good")
                    entry = zipfile.ZipInfo("placeholder.txt")
                    entry.filename = name
                    zf.writestr(entry, b"evil")
                    zf.writestr("late.txt", b"late")
                self.assertIn(name.encode("ascii"), archive.read_bytes())
                destination = directory / "parent" / "release"
                destination.parent.mkdir()
                output = self.extract_with_deploy_function(archive, destination, expected=1)
                expected = [message + ": " + name]
                if message == "Archive entry is not a valid path":
                    expected.append(unreadable)  # Windows PowerShell 5.1 rejects the name while listing
                self.assertTrue(any(text in output for text in expected), output)
                self.assertFalse(destination.exists())
                self.assertEqual(sorted(path.name for path in directory.rglob("*")),
                                 ["crafted.zip", "parent"])

    def check_tagged_deploy(self, path_form=None, env=None):
        directory = fixture(self)
        parent = directory / "Deployment Parent With Long Name"
        parent.mkdir()
        canonical_parent = parent.resolve()
        target_parent = canonical_parent
        if path_form == "case":
            target_parent = Path(str(canonical_parent).swapcase())
            self.assertNotEqual(str(target_parent), str(canonical_parent))
        elif path_form == "short":
            import ctypes
            from ctypes import wintypes
            get_short_path = ctypes.windll.kernel32.GetShortPathNameW
            get_short_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
            get_short_path.restype = wintypes.DWORD
            size = get_short_path(str(canonical_parent), None, 0)
            if not size:
                self.skipTest("An 8.3 short path is unavailable")
            buffer = ctypes.create_unicode_buffer(size)
            written = get_short_path(str(canonical_parent), buffer, size)
            if not written or written >= size:
                self.skipTest("An 8.3 short path could not be obtained")
            target_parent = Path(buffer.value)
            if str(target_parent).casefold() == str(canonical_parent).casefold():
                self.skipTest("The filesystem did not produce a distinct 8.3 short path")
            self.assertNotEqual(len(str(target_parent)), len(str(canonical_parent)))
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
        (checkout / "empty.bin").write_bytes(b"")
        (checkout / "nested").mkdir()
        (checkout / "nested" / "binary.bin").write_bytes(bytes(range(256)))
        git("add", "sample.txt", "empty.bin", "nested/binary.bin")
        git("-c", "user.name=Dummy", "-c", "user.email=dummy@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-m", "Dummy fixture")
        git("tag", "v-test")
        commit = git("rev-parse", "HEAD")
        (checkout / "sample.txt").write_text("dirty\n", encoding="utf-8")
        target = target_parent / "release"
        archive = directory / "archives"
        script = ROOT / "deploy/deploy.ps1"
        args = ("-Checkout", checkout, "-Tag", "v-test", "-Target", target,
                "-ArchiveDirectory", archive)
        self.run_ps(script, *args, "-WhatIf", env=env)
        self.assertFalse(target.exists())
        self.assertFalse(archive.exists())
        output = self.run_ps(script, *args, env=env)
        manifest = json.loads((target / "DEPLOYED.json").read_text(encoding="utf-8"))
        self.assertEqual(json.loads(output), manifest)
        self.assertEqual(manifest["tag"], "v-test")
        self.assertEqual(manifest["commit"], commit)
        self.assertEqual((target / "sample.txt").read_text(), "tagged\n")
        self.assertEqual(manifest["sha256"], {
            name: hashlib.sha256((target / name).read_bytes()).hexdigest()
            for name in ("sample.txt", "empty.bin", "nested/binary.bin")})
        self.run_ps(script, *args, expected=1, env=env)

    @unittest.skipUnless(shutil.which("git"), "git is unavailable")
    def test_deploy_checks_git_exit_codes_itself(self):
        # With this preference on, PowerShell 7 throws its own error for a failing native
        # command before deploy.ps1 can check $LASTEXITCODE. Windows PowerShell 5.1 ignores it.
        directory = fixture(self)
        wrapper = directory / "native_errors_on.ps1"
        wrapper.write_text(
            "param([string]$Script, [string]$Checkout, [string]$Target)\n"
            "$ErrorActionPreference = 'Stop'\n"
            "$PSNativeCommandUseErrorActionPreference = $true\n"
            "& $Script -Checkout $Checkout -Tag 'bad..tag' -Target $Target -WhatIf\n", encoding="utf-8")
        result = self.run_ps_result(wrapper, "-Script", ROOT / "deploy/deploy.ps1",
                                    "-Checkout", directory / "checkout", "-Target", directory / "release",
                                    expected=1)
        self.assertIn("Invalid tag name.", (result.stdout or "") + (result.stderr or ""))
        self.assertEqual(sorted(path.name for path in directory.iterdir()), ["native_errors_on.ps1"])

    def test_restart_compares_heartbeat_time_as_an_instant(self):
        # PowerShell 7 ConvertFrom-Json turns ts into a DateTime. Formatting that back to a
        # string dropped the fractional seconds and the offset, so a heartbeat written in the
        # same second as the process start looked older than the process. The process is
        # started just after a second boundary, and the launch is accepted only when the
        # clock read before and after it falls in the same whole second, so the fresh
        # heartbeat below always shares the start's second and is never earlier than it.
        import time
        directory = fixture(self)

        def stop(process):
            # The dummy process exits when its standard input is closed.
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

        attempts = 5
        child = None
        for _ in range(attempts):
            time.sleep(1.05 - time.time() % 1)
            before = time.time()
            child = subprocess.Popen([sys.executable, "-B", "-c", "import sys; sys.stdin.read()"],
                                     stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            observed = time.time()
            # The start time Windows records can lag the precise clock by one timer tick,
            # so the launch must also begin clearly after the second boundary.
            if int(before) == int(observed) and before % 1 >= 0.03:
                break
            stop(child)
            child = None
        if child is None:
            self.fail("The dummy process could not be started within one whole second in "
                      + str(attempts) + " attempts, so the sub-second case would not be covered")
        try:
            utc = datetime.fromtimestamp(observed, timezone.utc)
            forms = {
                "service writer form": lambda moment: moment.astimezone().isoformat(),
                "UTC with Z": lambda moment: moment.isoformat().replace("+00:00", "Z"),
                "UTC with offset": lambda moment: moment.isoformat(),
                "negative offset": lambda moment: moment.astimezone(
                    timezone(-timedelta(hours=8, minutes=30))).isoformat(),
            }
            script = ROOT / "deploy/windows/restart.ps1"
            args = ("-StateDirectory", directory, "-TimeoutSeconds", "10", "-WhatIf")
            heartbeat = directory / "heartbeat.json"
            for name, form in forms.items():
                with self.subTest(form=name):
                    heartbeat.write_text(json.dumps({"schema": "slack-dispatch-heartbeat/1", "ts": form(utc),
                                                     "pid": child.pid}), encoding="utf-8")
                    self.run_ps(script, *args)
                    heartbeat.write_text(json.dumps({"schema": "slack-dispatch-heartbeat/1",
                                                     "ts": form(utc - timedelta(hours=1)),
                                                     "pid": child.pid}), encoding="utf-8")
                    result = self.run_ps_result(script, *args, expected=1)
                    self.assertIn("Heartbeat predates this process", (result.stdout or "") + (result.stderr or ""))
                    self.assertFalse((directory / "stop.flag").exists())
            self.assertIsNone(child.poll())
        finally:
            stop(child)

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


@unittest.skipUnless(WINDOWS_POWERSHELL, "Windows PowerShell 5.1 (powershell.exe) is not installed")
class WindowsToolTests(WindowsToolChecks, unittest.TestCase):
    powershell = WINDOWS_POWERSHELL
    minimum_major = 5
    maximum_major = 5


@unittest.skipUnless(POWERSHELL_7, "PowerShell 7 (pwsh.exe) is not installed")
class WindowsToolTestsPowerShell7(WindowsToolChecks, unittest.TestCase):
    powershell = POWERSHELL_7
    minimum_major = 7


class WindowsToolAvailabilityTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "PowerShell is only required on Windows")
    def test_windows_tool_tests_run_under_at_least_one_powershell(self):
        self.assertTrue(WINDOWS_POWERSHELL or POWERSHELL_7,
                        "Neither powershell.exe nor pwsh.exe is installed, so no Windows tool test runs")


if __name__ == "__main__":
    unittest.main()
