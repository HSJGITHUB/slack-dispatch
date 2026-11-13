"""Run a configured local CLI with metadata only and without a shell."""
from datetime import datetime, timezone
import logging
import re
import shlex
import subprocess
import time

LOG = logging.getLogger(__name__)


class NotifyError(RuntimeError):
    """Notification failed; durable pending work must be retained."""


SENT_AT_FORMAT = "%m-%d %H:%M:%S"


def sent_at_zone(name):
    """Time zone for `{sent_at}`: the display_timezone name, UTC when unset or unavailable."""
    if not name or name == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
        return timezone.utc


def summary(lane, message_ids):
    return f"Lane {lane}: {len(message_ids)} new inbox item(s) (ids: {', '.join(message_ids)}). Run: inbox {lane}"


class SessionMessageNotifier:
    # Set by the adapter builder when target_resolver accepts a message_ids keyword.
    resolver_takes_ids = False

    def __init__(self, command_template, target_resolver, *, runner=subprocess.run,
                 dry_run=False, timeout=60, template=None, lanes=(), lookup=None,
                 display_timezone=None, clock=time.time):
        self.command_template = command_template
        self.target_resolver = target_resolver
        self.runner = runner
        self.dry_run = dry_run
        self.timeout = timeout
        self.template = dict(template or {})
        self.lanes = list(lanes)
        self.lookup = lookup
        # P14: `{sent_at}` is the render time in display_timezone (MM-DD HH:MM:SS).
        self.zone = sent_at_zone(display_timezone)
        self.clock = clock
        for key in ("message", "cli", "target"):
            if key in self.template and not isinstance(self.template[key], str):
                raise ValueError("Session template values must be strings")

    def message(self, lane, message_ids):
        if "message" not in self.template:
            return summary(lane, message_ids)
        counts = {"new": 0, "backfill": 0}
        attention = 0
        for mid in message_ids:
            row = (self.lookup(mid) if self.lookup else None) or {}
            counts["backfill" if row.get("origin") == "reconcile" else "new"] += 1
            attention += bool(row.get("attention"))
        values = {"lane": lane, "count": str(len(message_ids)), "ids": ", ".join(message_ids),
                  "kinds": ", ".join(f"{key} {count}" for key, count in counts.items() if count),
                  "attention": str(attention), "lanes": ", ".join(self.lanes),
                  "cli": self.template.get("cli", ""), "sent_at": self.sent_at()}
        return re.sub(r"\{([a-zA-Z_][a-zA-Z_0-9]*)\}",
                      lambda match: values.get(match[1], match[0]), self.template["message"])

    def sent_at(self):
        return datetime.fromtimestamp(self.clock(), self.zone).strftime(SENT_AT_FORMAT)

    def notify(self, lane, message_ids):
        if self.resolver_takes_ids:
            target = self.target_resolver(lane, message_ids=list(message_ids))
        else:
            target = self.target_resolver(lane)
        if target is None:
            LOG.info("Session notification skipped: no live session")
            return
        parts = shlex.split(self.command_template) if isinstance(self.command_template, str) else self.command_template
        if self.dry_run:
            names = sorted(set(re.findall(r"\{[a-zA-Z_][a-zA-Z_0-9]*\}",
                                          " ".join(parts) + " " + self.template.get("message", ""))))
            LOG.info("Dry-run command placeholders: %s", names)
            return
        message = self.message(lane, message_ids)
        def render(part, target):
            values = {"{target}": target, "{message}": message}
            return re.sub(r"\{(?:target|message)\}", lambda match: values[match[0]], part)

        command = [render(part, target) for part in parts]
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
