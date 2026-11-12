"""Run a configured local CLI with metadata only and without a shell."""
import logging
import re
import shlex
import subprocess

LOG = logging.getLogger(__name__)


class NotifyError(RuntimeError):
    """Notification failed; durable pending work must be retained."""


def summary(lane, message_ids):
    return f"Lane {lane}: {len(message_ids)} new inbox item(s) (ids: {', '.join(message_ids)}). Run: inbox {lane}"


class SessionMessageNotifier:
    def __init__(self, command_template, target_resolver, *, runner=subprocess.run,
                 dry_run=False, timeout=60):
        self.command_template = command_template
        self.target_resolver = target_resolver
        self.runner = runner
        self.dry_run = dry_run
        self.timeout = timeout

    def notify(self, lane, message_ids):
        target = self.target_resolver(lane)
        if target is None:
            LOG.info("Session notification skipped: no live session")
            return
        parts = shlex.split(self.command_template) if isinstance(self.command_template, str) else self.command_template
        def render(part, target):
            values = {"{target}": target, "{message}": summary(lane, message_ids)}
            return re.sub(r"\{(?:target|message)\}", lambda match: values[match[0]], part)

        command = [render(part, target) for part in parts]
        if self.dry_run:
            # Templates and targets must be credential-free. Log only generated arguments.
            LOG.info("Dry-run command: %s", [render(part, "<target>")
                     if "{target}" in part or "{message}" in part else "<literal>" for part in parts])
            return
        for attempt in range(2):
            try:
                result = self.runner(command, timeout=self.timeout, check=True,
                                     capture_output=True, shell=False)
                if getattr(result, "returncode", 0):
                    raise NotifyError("Session command failed")
                return
            except (OSError, subprocess.SubprocessError, NotifyError):
                if attempt:
                    raise NotifyError("Session notification failed after two attempts") from None
