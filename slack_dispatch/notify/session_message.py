"""Run a configured local CLI with metadata only and without a shell."""
from datetime import datetime, timezone
import json
import logging
import re
import shlex
import subprocess
import time

from .base import error_detail

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


# P18: relay output capture for the delivery log.
UNPARSED_CHARS = 200
SCRUB_MIN_CHARS = 24


def relay_result(stdout):
    """The relay's `--output-format json` result object from stdout, or None.

    Accepts one JSON object, or a JSON list whose last `type: result` object is used.
    """
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    if not isinstance(stdout, str) or not stdout.strip():
        return None
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    if isinstance(data, list):
        data = next((item for item in reversed(data)
                     if isinstance(item, dict) and item.get("type") == "result"), None)
    return data if isinstance(data, dict) else None


def _normalized(text):
    # `"success": true` and `"success":true` match the same pattern; case is ignored.
    return re.sub(r'"\s*:\s*', '":', text).casefold()


def classify_relay(text, delivered=(), not_delivered=()):
    """`delivered`, `not_delivered` or `unknown` for the relay result text.

    Patterns are case-insensitive substrings. When patterns of both lists match, the one
    found earliest in the text wins, so a success text that explains what happens if the
    session "refuses it" stays delivered (no default pattern names a refusal).
    """
    if not isinstance(text, str) or not text:
        return "unknown"
    haystack = _normalized(text)
    found = []
    for outcome, patterns in (("delivered", delivered), ("not_delivered", not_delivered)):
        for pattern in patterns:
            position = haystack.find(_normalized(pattern))
            if position >= 0:
                found.append((position, outcome))
    return min(found)[1] if found else "unknown"


def _scrub(text, command):
    """Replace any long rendered command part (the wake text) echoed by the relay."""
    for part in command:
        if isinstance(part, str) and len(part) >= SCRUB_MIN_CHARS and part in text:
            text = text.replace(part, "[message]")
    return text


def relay_fields(stdout, command, delivered=(), not_delivered=()):
    """Delivery log fields (status, detail, cost, duration, relay session) from stdout."""
    data = relay_result(stdout)
    if data is None:
        raw = stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else (stdout or "")
        raw = _scrub(str(raw), command)[:UNPARSED_CHARS]
        return {"status": "sent", "detail": ("unparsed output | " + raw).rstrip(" |")}
    text = data.get("result")
    text = _scrub(text, command) if isinstance(text, str) else ""
    outcome = classify_relay(text, delivered, not_delivered)
    cost = data.get("total_cost_usd")
    duration = data.get("duration_ms")
    session = data.get("session_id")
    return {"status": "failed" if data.get("is_error") is True else "sent",
            "detail": f"{outcome} | {text}".rstrip(" |"),
            "cost_usd": float(cost) if isinstance(cost, (int, float))
            and not isinstance(cost, bool) else None,
            "duration_ms": int(duration) if isinstance(duration, (int, float))
            and not isinstance(duration, bool) else None,
            "relay_session_id": session if isinstance(session, str) else None}


def summary(lane, message_ids):
    return f"Lane {lane}: {len(message_ids)} new inbox item(s) (ids: {', '.join(message_ids)}). Run: inbox {lane}"


class SessionMessageNotifier:
    # Set by the adapter builder when target_resolver accepts a message_ids keyword.
    resolver_takes_ids = False
    # P18: this adapter writes its own delivery_log rows through `delivery_recorder`,
    # which the pipeline sets; without one nothing is recorded.
    logs_deliveries = True
    delivery_recorder = None

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

    def skip_status(self, lane):
        """Delivery log status when the resolver gave no target (P18)."""
        resolver = getattr(self, "session_resolver", None)
        state = getattr(resolver, "lane_state", {}).get(lane) if resolver is not None else None
        return "skipped_ambiguous" if state == "ambiguous" else "skipped_no_session"

    def target_session(self, lane, target):
        """(session id or None, session name) of the addressed session, for the delivery log."""
        resolver = getattr(self, "session_resolver", None)
        lookup = getattr(resolver, "resolved_session", None)
        if callable(lookup):
            try:
                return lookup(lane, target)
            except Exception:
                return None, target
        return None, target

    def sent_at(self):
        return datetime.fromtimestamp(self.clock(), self.zone).strftime(SENT_AT_FORMAT)

    def notify(self, lane, message_ids):
        if self.resolver_takes_ids:
            target = self.target_resolver(lane, message_ids=list(message_ids))
        else:
            target = self.target_resolver(lane)
        recorder = self.delivery_recorder
        if target is None:
            LOG.info("Session notification skipped: no live session")
            if recorder is not None:
                recorder.record(lane, message_ids, self.skip_status(lane))
            return
        session_id, session_name = self.target_session(lane, target)
        parts = shlex.split(self.command_template) if isinstance(self.command_template, str) else self.command_template
        if self.dry_run:
            names = sorted(set(re.findall(r"\{[a-zA-Z_][a-zA-Z_0-9]*\}",
                                          " ".join(parts) + " " + self.template.get("message", ""))))
            LOG.info("Dry-run command placeholders: %s", names)
            if recorder is not None:
                recorder.record(lane, message_ids, "dry_run", target_session_id=session_id,
                                target_session_name=session_name)
            return
        message = self.message(lane, message_ids)
        # P18: the row is stamped with the relay start, so an ack during the relay counts.
        started = recorder.now() if recorder is not None else None
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
                if recorder is not None:
                    try:
                        fields = relay_fields(getattr(result, "stdout", None), command,
                                              recorder.delivered_patterns,
                                              recorder.not_delivered_patterns)
                    except Exception as error:
                        # Reading the relay output must never turn a delivery into a failure.
                        fields = {"status": "sent",
                                  "detail": "relay output not read | " + type(error).__name__}
                    recorder.record(lane, message_ids, target_session_id=session_id,
                                    target_session_name=session_name, at=started, **fields)
                return
            except (OSError, subprocess.SubprocessError, NotifyError) as error:
                if attempt:
                    if recorder is not None:
                        status = "timeout" if isinstance(error, subprocess.TimeoutExpired) else "failed"
                        recorder.record(lane, message_ids, status, detail=error_detail(error),
                                        target_session_id=session_id,
                                        target_session_name=session_name, at=started)
                    raise NotifyError("Session notification failed after two attempts") from None
