# Operations

## Integration status

This package supplies independent runtime and deployment building blocks.
The current slack_dispatch.service is a scaffold raising NotImplementedError.
Registering its task does not make it operational. Service integration remains
a separate change. No production service or live scheduler registration is claimed.

The integrated service must hold SingleInstanceLock(state / "service.lock")
throughout its lifetime, configure logging, and call HeartbeatWriter.write()
on loop iterations. On ControlFlags.should_stop(), stop sources, flush durable
state, acknowledge_stop() after orderly shutdown, release the lock and exit.
Check wake_enabled() before waking a lane. Intake can continue with wakes off.

## Runtime interfaces

- SingleInstanceLock(path) uses msvcrt on Windows and flock elsewhere. Byte zero
  is reserved; remaining ASCII bytes record the PID. AlreadyRunning.pid reports
  that PID (possibly None during initial publication). The OS releases ownership
  after a crash. The persistent file alone does not indicate a running process.
- HeartbeatWriter(path, interval_seconds, clock) accepts a clock callable returning
  Unix seconds or a datetime. write(ok=True, status="running", version="unknown",
  counters=None, force=False) publishes immediately, then at the given interval,
  returning whether it wrote. No background thread is started. Backward clock
  adjustments permit immediate publication. Supply the application version.
- Heartbeat JSON: schema is slack-dispatch-heartbeat/1; ts is local ISO time with
  offset; pid is the writer; ok is health; status is a short operational label;
  version is the caller's version; counters holds metadata counts only.
  read_heartbeat(path) returns a dictionary or None. is_stale(hb, now,
  max_age_seconds) treats missing, malformed, wrong-schema and future-dated
  timestamps as stale. Exactly max_age_seconds old is not stale. Health and
  staleness are separate checks.
- Publication uses a same-directory temporary file, flush, fsync and replace.
  Failed publication may retain its temporary file for diagnosis.
- stop.flag requests shutdown; wake.off disables wakes. Restore wakes manually (file move) by moving
  wake.off into done/. Acknowledged stop flags move to unique timestamped names
  under done/. They are not deleted.
- configure_logging(log_dir, level) configures the slack_dispatch logger and
  descendants with local-time midnight rotation. Repeated setup replaces only
  runtime-owned handlers. Rotated files are retained for operator archival.
  Slack-shaped bot/user/app token strings are masked, including exception output.
  Attributes named text or body and matching dictionary keys are removed.
  Never interpolate message bodies into arbitrary strings or objects for logging.

## Windows operator steps

Use Windows PowerShell 5.1 and a Python installation with dependencies already
available. All example paths and user names below are dummy values. No network
access is performed by these scripts.

1. Complete service integration and offline acceptance before activating a task.
   Preview a release with:
   deploy.ps1 -Checkout C:\demo\checkout -Tag v0.1.0 -Target C:\demo\release -WhatIf
   Remove WhatIf to extract the tag into a new directory. Existing targets,
   symlinks, submodules and tracked DEPLOYED.json files are refused.
   Dirty and untracked checkout files are not deployed. Git archive attributes
   apply, including export-ignore/export-subst.
2. Deployment prints and writes DEPLOYED.json with tag, resolved commit, local
   time and SHA-256 of every extracted file. It excludes the manifest itself,
   since a file cannot contain its own stable hash. Archives remain in
   ArchiveDirectory (TEMP by default). Partial failures can leave a target with
   no manifest; inspect and move it before retrying. Deployment does not stop a
   service, switch a task, install dependencies or copy credentials.
3. Keep state and operator-supplied config outside release directories. Render:
   windows\install_task.ps1 -Python C:\demo\python\pythonw.exe -Workdir C:\demo\release -Config C:\demo\state\config.yaml -User DEMO\operator
   XML values are escaped. OutputDirectory defaults to TEMP. The script writes
   unique XML and prints a schtasks.exe /Create command only. The operator
   reviews XML and manually runs the command. WhatIf prints a proposed command
   but writes no XML; that preview command is not ready to execute.
   Rendered XML uses UTF-16 LE with BOM as required by Task Scheduler; operators can verify with `schtasks /Query /TN <name> /XML`.
   The template stays UTF-8 on disk because it is read as text before rendering.
4. The task runs pythonw -X utf8 -B -m slack_dispatch.service --config ... at
   logon and repeats every minute while the interactive user remains logged on.
   IgnoreNew avoids overlapping task runs; runtime locking protects manual
   launches too. There is no execution time limit. This is not boot-time or
   logged-out supervision. A newly registered task may need the next logon or a
   manual operator start.
5. Preview restart with:
   windows\restart.ps1 -StateDirectory C:\demo\state -WhatIf
   Run without WhatIf to read heartbeat.json, capture the original process,
   reject a missing process or detected PID reuse, write stop.flag, and wait up
   to 120 seconds. Timeout reports failure and retains the flag. The script
   never kills or launches a process. Scheduler repetition must launch the next
   instance. The operator verifies a new healthy heartbeat. PID and timestamp
   alone cannot exclude every unrelated process; protect the state directory.

All write scripts support WhatIf. Registration and release activation are manual.
To keep a service stopped, disable the task first. Under this supervision policy
stop.flag alone requests a restart. Inspect an unacknowledged flag rather than
silently deleting it. Release switching and rollback require the operator to
render and register the task with the selected release directory.