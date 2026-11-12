"""Service orchestration with transient routing text and metadata-only delivery."""
from dataclasses import dataclass
import logging
from pathlib import Path
import threading

from slack_dispatch.notify.wake import Deliver
from slack_dispatch.queue.human import export_jsonl
from slack_dispatch.router.apply import apply_decision
from slack_dispatch.router.engine import route

LOG = logging.getLogger(__name__)
HUMAN_QUEUE = "human_queue"


def adapter_entries(config, notifiers, purpose):
    """The existing builder returns adapters in configuration order."""
    return [(index, spec, notifier)
            for index, (spec, notifier) in enumerate(zip(config.notify_adapters,
                                                       notifiers, strict=True))
            if spec.purpose == purpose]


def can_notify(config, store, spec, lane):
    if spec.kind != "session_message":
        return True
    if not config.wake_policy.enabled:
        return False
    if (Path(config.service.state_dir) / "wake.off").exists():
        return False
    current = store.get_lane(lane)
    return bool(current and not current.get("paused") and current.get("session_pointer"))


@dataclass(frozen=True)
class HandleResult:
    message_id: str | None
    kind: str
    duplicate: bool = False
    state: str | None = None
    lane: str | None = None


class Pipeline:
    def __init__(self, config, store, suggester, notifiers, wake_engine, clock):
        self.config, self.store, self.suggester = config, store, suggester
        self.notifiers = list(notifiers)
        self.wake_engine, self.clock = wake_engine, clock
        self.lock = threading.RLock()
        self.counters = dict(events=0, routed=0, human_pending=0, delivered=0, failures=0)
        self._missing_human_logged = False
        self._retry = {}
        self._export_pending = False
        # Keep the package's v1 migration compatible. This table is service-owned.
        with store._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS service_deliveries (
                source TEXT NOT NULL, channel TEXT NOT NULL, thread_ts TEXT NOT NULL,
                lane TEXT NOT NULL, delivered_at REAL NOT NULL,
                PRIMARY KEY (source, channel, thread_ts, lane))""")

    def failure(self, operation):
        with self.lock:
            self.counters["failures"] += 1
            LOG.error("Service operation failed: %s", operation)

    def snapshot(self):
        with self.lock:
            return dict(self.counters)

    def _thread_key(self, message, lane):
        return (message["source"], message["channel"],
                message.get("thread_ts") or message["ts"], lane)

    def _recent_delivery(self, message, lane):
        rows = self.store._rows(
            "SELECT delivered_at FROM service_deliveries "
            "WHERE source=? AND channel=? AND thread_ts=? AND lane=?",
            self._thread_key(message, lane))
        return bool(rows and 0 <= self.clock() - rows[0]["delivered_at"]
                    <= self.config.service.followup_window_seconds)

    def _record_delivery(self, lane, ids):
        now = self.clock()
        with self.store._transaction() as db:
            for mid in ids:
                message = self.store.get_message(mid)
                if message is not None:
                    db.execute("""INSERT INTO service_deliveries VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(source, channel, thread_ts, lane)
                        DO UPDATE SET delivered_at=excluded.delivered_at""",
                               (*self._thread_key(message, lane), now))

    def _send_one(self, index, lane, ids):
        spec, notifier = self.config.notify_adapters[index], self.notifiers[index]
        if not can_notify(self.config, self.store, spec, lane):
            return False
        try:
            notifier.notify(lane, list(ids))
        except Exception:
            self._retry.setdefault((index, lane), set()).update(ids)
            self.failure("notify")
            return False
        pending = self._retry.get((index, lane))
        if pending is not None:
            pending.difference_update(ids)
            if not pending:
                del self._retry[index, lane]
        return getattr(notifier, "service_dry_run", False) is not True

    def _send(self, purpose, lane, ids, *, inbox=None):
        entries = adapter_entries(self.config, self.notifiers, purpose)
        if purpose == "human_queue" and not entries and not self._missing_human_logged:
            LOG.warning("No human queue notifier configured")
            self._missing_human_logged = True
        sent = False
        for index, spec, _ in entries:
            if inbox is not None and (spec.kind == "file_inbox") != inbox:
                continue
            sent = self._send_one(index, lane, ids) or sent
        if sent:
            self.counters["delivered"] += len(ids)
            if purpose == "lane":
                self._record_delivery(lane, ids)

    def _export_queue(self):
        self._export_pending = True
        try:
            path = Path(self.config.service.state_dir) / "human_queue.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            export_jsonl(self.store, path)
        except Exception:
            self.failure("queue_export")
        else:
            self._export_pending = False

    def handle_event(self, event) -> HandleResult:
        with self.lock:
            self.counters["events"] += 1
            try:
                return self._handle(event)
            except Exception:
                self.failure("event")
                # Reconcile must not advance its cursor after a failed callback.
                raise RuntimeError("Event processing failed") from None

    def _handle(self, event):
        recorded = self.store.record_event(event)
        mid = recorded.message_id
        if recorded.duplicate or recorded.kind != "message":
            return HandleResult(mid, recorded.kind, recorded.duplicate)
        message = self.store.get_message(mid) if mid else None
        if message is None or event.get("subtype") == "message_deleted":
            return HandleResult(mid, recorded.kind)
        if "text" in event:
            message["text"] = event["text"]
        decision = route(message, self.config, self.store.lookup_binding)
        outcome = apply_decision(self.store, mid, decision, self.suggester)
        self.counters["routed"] += 1
        if outcome.state == "assigned":
            lane = outcome.lane
            # The router binds replies only. Bind new roots for later inheritance.
            key = self._thread_key(message, lane)[:3]
            if self.store.lookup_binding(*key) is None:
                self.store.bind_thread(*key, lane)
            reply = bool(message.get("thread_ts") and message["thread_ts"] != message["ts"])
            if (event.get("subtype") == "message_changed"
                    or (reply and self._recent_delivery(message, lane))):
                self.store.mark_followup(mid)
                message["followup"] = True
            # Durable inboxes must survive disabled, deferred and batched wakes.
            self._send("lane", lane, [mid], inbox=True)
            wake = self.wake_engine.offer(lane, message, message.get("author"))
            if isinstance(wake, Deliver):
                self._send("lane", lane, [mid], inbox=False)
            if getattr(wake, "notice", None) is not None:
                self._send("human_queue", HUMAN_QUEUE, [mid])
                self.wake_engine.notices.clear()
        elif outcome.state == "human_pending":
            self.counters["human_pending"] += 1
            self._export_queue()
            self._send("human_queue", HUMAN_QUEUE, [mid])
        return HandleResult(mid, recorded.kind, state=outcome.state, lane=outcome.lane)

    def flush(self, now):
        with self.lock:
            for (index, lane), pending in list(self._retry.items()):
                spec = self.config.notify_adapters[index]
                ids = self._current_ids(lane, pending, spec.purpose)
                if not ids:
                    self._retry.pop((index, lane), None)
                    continue
                if spec.purpose == "lane" and spec.kind != "file_inbox" and not self.wake_engine.enabled():
                    continue
                if self._send_one(index, lane, ids):
                    self.counters["delivered"] += len(ids)
                    if spec.purpose == "lane":
                        self._record_delivery(lane, ids)
            if self._export_pending:
                self._export_queue()
            for lane, ids in self.wake_engine.flush(now):
                ids = self._current_ids(lane, ids, "lane")
                if ids:
                    self._send("lane", lane, ids, inbox=False)

    def _current_ids(self, lane, ids, purpose):
        result = []
        for mid in sorted(ids):
            row = self.store.get_message(mid)
            if row and (purpose == "human_queue" or
                        (row.get("lane") == lane and row["state"] == "assigned")):
                result.append(mid)
        return result
