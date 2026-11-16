"""Notifier contract for durable files, local session CLI wakes or assistant DM outboxes.

The lane selects the destination and payloads contain ledger ids only; failures must
surface to the caller while durable pending work remains available for retry.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import time
from typing import Protocol

LOG = logging.getLogger(__name__)


class Notifier(Protocol):
    def notify(self, lane: str, message_ids: list[str]) -> None: ...


# P18 delivery log vocabulary (ledger table delivery_log).
DELIVERY_STATUSES = ("sent", "failed", "skipped_no_session", "skipped_ambiguous",
                     "skipped_paused", "dry_run", "timeout")
RELAY_OUTCOMES = ("delivered", "not_delivered", "unknown")
DETAIL_CHARS = 500


class DeliveryRecorder:
    """Write one delivery_log row per attempt to reach a session or the assistant DM (P18).

    The pipeline creates one recorder per non-file_inbox adapter and sets it as the
    adapter's `delivery_recorder` when the adapter logs its own attempts
    (`logs_deliveries = True`: the session relay and the card writer). Shallow adapter
    copies (timer reminders, human notices) share the recorder. `context` carries the
    kind and the covered ids of the attempt the pipeline is making; without a context the
    attempt comes from the timer sweep, so its kind is `reminder`. A failed write is
    logged by class name and never breaks a delivery. Rows hold ids and metadata only.
    """

    def __init__(self, store, *, purpose, adapter, clock=time.time, delivered_patterns=(),
                 not_delivered_patterns=()):
        self.store, self.purpose, self.adapter, self.clock = store, purpose, adapter, clock
        self.delivered_patterns = tuple(delivered_patterns)
        self.not_delivered_patterns = tuple(not_delivered_patterns)
        self.context = None

    @contextmanager
    def attempt(self, kind, ids=None):
        """Mark the pipeline attempt (kind, covered ids) for rows written inside the block."""
        previous = self.context
        self.context = {"kind": kind, "ids": list(ids) if ids is not None else None}
        try:
            yield self
        finally:
            self.context = previous

    def now(self):
        """The recorder clock (epoch seconds), or None when it cannot be read."""
        try:
            return float(self.clock())
        except Exception:
            return None

    def record(self, lane, ids, status, *, kind=None, detail=None, target_session_id=None,
               target_session_name=None, cost_usd=None, duration_ms=None, relay_session_id=None,
               at=None):
        """Write one row. `at` (epoch seconds) is the attempt start, for example the moment
        the relay was started; without it the row is stamped with the current clock."""
        writer = getattr(self.store, "add_delivery", None)
        if not callable(writer):
            return None
        context = self.context or {}
        kind = kind or context.get("kind") or "reminder"
        covered = context.get("ids") if context.get("ids") is not None else list(ids)
        try:
            created = datetime.fromtimestamp(float(self.clock() if at is None else at), timezone.utc)
            return writer({"created_at": created.isoformat(timespec="microseconds"),
                           "purpose": self.purpose, "lane": lane, "kind": kind,
                           "adapter": self.adapter, "target_session_id": target_session_id,
                           "target_session_name": target_session_name,
                           "item_count": len(covered), "item_ids": [str(mid) for mid in covered],
                           "status": status,
                           "detail": detail[:DETAIL_CHARS] if isinstance(detail, str) else None,
                           "cost_usd": cost_usd, "duration_ms": duration_ms,
                           "relay_session_id": relay_session_id})
        except Exception as error:
            LOG.error("Delivery log write failed class=%s", type(error).__name__)
            return None


def error_detail(error):
    """Error class and a short reason without the exception text (it can carry the command)."""
    name = type(error).__name__
    returncode = getattr(error, "returncode", None)
    if isinstance(returncode, int):
        return f"{name}: exit status {returncode}"
    timeout = getattr(error, "timeout", None)
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
        return f"{name}: no answer within {timeout:g}s"
    errno = getattr(error, "errno", None)
    if isinstance(errno, int):
        return f"{name}: errno {errno}"
    return name
