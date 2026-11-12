"""Service orchestration with transient routing text and metadata-only delivery."""
from dataclasses import dataclass
from decimal import Decimal
from collections import Counter
import logging
from pathlib import Path
import threading
import sys

from slack_dispatch.notify.wake import Deliver
from slack_dispatch.notify import notify_human
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
    if spec.purpose == "lane" and spec.kind != "file_inbox":
        if not config.wake_policy.enabled or (Path(config.service.state_dir) / "wake.off").exists():
            return False
    if spec.kind != "session_message":
        return True
    current = store.get_lane(lane)
    return bool(current and not current.get("paused") and current.get("session_pointer"))


class HumanQueueDelivery:
    """Durable ids-only digest backlog and per-adapter one-shot receipts."""
    def __init__(self, config, store, notifiers, clock):
        self.config, self.store, self.notifiers, self.clock = config, store, notifiers, clock
        self.failures = 0
        self.delivered = 0

    def offer(self, mid, kind="message", *, immediate=False, adapter=None, now=None):
        now = self.clock() if now is None else now
        with self.store._transaction() as db:
            for index, spec, _ in adapter_entries(self.config, self.notifiers, "human_queue"):
                if adapter is not None and index != adapter:
                    continue
                db.execute("INSERT OR IGNORE INTO human_notices "
                           "(message_id, kind, adapter, queued_at, immediate) VALUES (?, ?, ?, ?, ?)",
                           (mid, kind, index, now, immediate))
        if immediate or self.config.human_queue.mode == "immediate":
            return self.flush(now, immediate_only=True)
        return 0

    def _send(self, index, rows, now, digest):
        spec, notifier = self.config.notify_adapters[index], self.notifiers[index]
        if not can_notify(self.config, self.store, spec, HUMAN_QUEUE):
            return 0
        messages = {row["message_id"]: self.store.get_message(row["message_id"]) for row in rows}
        ids = sorted(messages)[:20]
        counts = dict(sorted(Counter(row["channel"] for row in messages.values()).items()))
        commands = "Run: python -m slack_dispatch.inbox.cli queue; python -m slack_dispatch.inbox.cli assign ID LANE"
        title = "Human queue digest" if digest else "Human queue notice"
        assigned = sorted({f"FYI, assigned to lane {row['lane']}" for row in messages.values()
                           if row.get("attention") and row["state"] == "assigned"})
        timer_counts = dict(Counter(row["kind"] for row in rows
                                    if row["kind"] in ("unassigned", "reassign")))
        timer_summary = "; ".join(
            f"{'reassignment proposal' if kind == 'reassign' else kind}={count}"
            for kind, count in sorted(timer_counts.items()))
        summary = (f"{title}: {len(messages)} item(s); channels: "
                   + ", ".join(f"{channel}={count}" for channel, count in counts.items())
                   + "; ids: " + ", ".join(ids) + ". " + "; ".join(assigned)
                   + ("; timers: " + timer_summary if timer_summary else "")
                   + "\n" + commands)
        metadata = dict(count=len(messages), channels=counts, ids=ids, summary=summary,
                        timer_counts=timer_counts,
                        timers=sorted({row["kind"] for row in rows
                                       if row["kind"] in ("unassigned", "reassign")}))
        try:
            if spec.dry_run:
                LOG.info("Dry-run human notice: %s", summary)
                return 0
            notify_human(notifier, ids, metadata)
            if getattr(notifier, "service_dry_run", False) is True:
                return 0
            with self.store._transaction() as db:
                for row in rows:
                    db.execute("UPDATE human_notices SET sent_at=? WHERE message_id=? AND kind=? AND adapter=?",
                               (now, row["message_id"], row["kind"], index))
                    if row["kind"] in ("unassigned", "reassign"):
                        db.execute("INSERT OR IGNORE INTO timer_events VALUES (?, ?, ?, ?)",
                                   (row["message_id"], row["kind"], index, now))
            self.delivered += len({row["message_id"] for row in rows
                                   if row["kind"] not in ("unassigned", "reassign")})
            return len(rows)
        except Exception as error:
            self.failures += 1
            LOG.error("Pipeline exception operation=human_notify class=%s", type(error).__name__)
            return 0

    def flush(self, now, *, immediate_only=False):
        sent = 0
        for index, _, _ in adapter_entries(self.config, self.notifiers, "human_queue"):
            rows = self.store._rows("SELECT * FROM human_notices WHERE adapter=? AND sent_at IS NULL "
                                    "ORDER BY queued_at, message_id, kind", (index,))
            active = []
            for row in rows:
                message = self.store.get_message(row["message_id"])
                valid = message and message["state"] not in ("done", "acked", "excluded")
                if row["kind"] in ("unassigned", "reassign") and self.store._rows(
                        "SELECT 1 FROM timer_events WHERE message_id=? AND timer=? AND adapter=?",
                        (row["message_id"], row["kind"], index)):
                    valid = False
                if row["kind"] in ("message", "unassigned"):
                    valid = valid and message["state"] in ("new", "human_pending")
                if row["kind"] == "reassign":
                    valid = valid and message["state"] == "assigned" and not message.get("ack_at")
                if valid:
                    active.append(row)
                else:
                    with self.store._transaction() as db:
                        db.execute("DELETE FROM human_notices WHERE message_id=? AND kind=? AND adapter=?",
                                   (row["message_id"], row["kind"], index))
            immediate, batch = [], []
            for row in active:
                (immediate if row["immediate"] or self.config.human_queue.mode == "immediate"
                 else batch).append(row)
            for row in immediate:
                sent += self._send(index, [row], now, False)
            if not immediate_only and batch and now >= batch[0]["queued_at"] + self.config.human_queue.digest_seconds:
                sent += self._send(index, batch, now, True)
        return sent


@dataclass(frozen=True)
class HandleResult:
    message_id: str | None
    kind: str
    duplicate: bool = False
    state: str | None = None
    lane: str | None = None
    backfill: bool = False


class Pipeline:
    def __init__(self, config, store, suggester, notifiers, wake_engine, clock):
        self.config, self.store, self.suggester = config, store, suggester
        self.notifiers = list(notifiers)
        self.wake_engine, self.clock = wake_engine, clock
        self.lock = threading.RLock()
        self.counters = dict(events=0, routed=0, human_pending=0, delivered=0, failures=0,
                             duplicates=0, prehandled=0, hydrated=0)
        self.reconcile_source = None
        self.last_event_at = None
        self._missing_human_logged = False
        self._retry = {}
        self._export_pending = False
        self.human = HumanQueueDelivery(config, store, self.notifiers, clock)
        # Keep the package's v1 migration compatible. This table is service-owned.
        with store._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS service_deliveries (
                source TEXT NOT NULL, channel TEXT NOT NULL, thread_ts TEXT NOT NULL,
                lane TEXT NOT NULL, delivered_at REAL NOT NULL,
                PRIMARY KEY (source, channel, thread_ts, lane))""")

    def failure(self, operation):
        with self.lock:
            self.counters["failures"] += 1
            error = sys.exc_info()[1]
            LOG.error("Pipeline exception operation=%s class=%s", operation,
                      type(error).__name__ if error else "Unknown")

    def snapshot(self):
        with self.lock:
            return dict(self.counters, failures=self.counters["failures"] + self.human.failures,
                        delivered=self.counters["delivered"] + self.human.delivered)

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
        return sent

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
            if event.get("origin") == "socket":
                # Gap detection measures live intake only; backfill must not mask a gap.
                self.last_event_at = event.get("received_at")
            try:
                return self._handle(event)
            except Exception:
                self.failure("event")
                # Reconcile must not advance its cursor after a failed callback.
                raise RuntimeError("Event processing failed") from None

    def _handle(self, event):
        event = dict(event, is_bot=bool(event.get("is_bot") or event.get("bot_id")
                     or not event.get("user") or event.get("user") in self.config.bot_messages.known_bot_users))
        recorded = self.store.record_event(event)
        mid = recorded.message_id
        if recorded.duplicate:
            self.counters["duplicates"] += 1
        if not recorded.duplicate and recorded.kind == "reaction_added":
            self._owner_reaction(event, mid)
        if recorded.duplicate or recorded.kind != "message":
            return HandleResult(mid, recorded.kind, recorded.duplicate)
        message = self.store.get_message(mid) if mid else None
        if message is None or event.get("subtype") == "message_deleted":
            return HandleResult(mid, recorded.kind)
        self._hydrate(event)
        owner = self.config.always_human.owner_user
        note = None
        if owner and event.get("user") == owner and self.config.owner_signals.posts == "record":
            note = "owner_post"
        elif owner and event.get("origin") == "reconcile":
            for reaction in event.get("reactions", []) or []:
                if (reaction.get("name") in self.config.owner_signals.reactions_done
                        and owner in reaction.get("users", [])):
                    note = "prehandled:reaction:" + reaction["name"]
                    break
            if note is None:
                later_owner = any(Decimal(row["ts"]) > Decimal(event["ts"]) for row in self.store._rows(
                    "SELECT ts FROM messages WHERE source=? AND channel=? AND author=? AND thread_ts=?",
                    (event["source"], event["channel"], owner, event.get("thread_ts") or event["ts"])))
                if (event.get("prehandled_by") or "").startswith("owner_reply:") or later_owner:
                    note = "prehandled:owner_reply"
        if note:
            self.store.set_route(mid, message.get("lane"), "owner_post" if note == "owner_post" else "prehandled", state="done")
            self.store.set_state(mid, "done", note=note)
            if note.startswith("prehandled:"):
                self.counters["prehandled"] += 1
            return HandleResult(mid, recorded.kind, state="done", lane=message.get("lane"))
        if "text" in event:
            message["text"] = event["text"]
        message["is_bot"] = event["is_bot"]
        decision = route(message, self.config, self.store.lookup_binding)
        outcome = apply_decision(self.store, mid, decision, self.suggester)
        self.counters["routed"] += 1
        backfill = False
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
            inboxed = self._send("lane", lane, [mid], inbox=True)
            if event.get("origin") == "reconcile":
                # Backfill fills durable inboxes only: no session wake, no storm accounting.
                backfill = inboxed
            else:
                wake = self.wake_engine.offer(lane, message, message.get("author"))
                if isinstance(wake, Deliver):
                    self._send("lane", lane, [mid], inbox=False)
                if getattr(wake, "notice", None) is not None:
                    self._human_notice(mid, kind="storm")
                    self.wake_engine.notices.clear()
        elif outcome.state == "human_pending":
            self.counters["human_pending"] += 1
            self._export_queue()
            self._human_notice(mid, attention=decision.attention)
        if outcome.state == "assigned" and decision.attention:
            self._human_notice(mid, attention=decision.attention)
        return HandleResult(mid, recorded.kind, state=outcome.state, lane=outcome.lane, backfill=backfill)

    def _owner_reaction(self, event, mid):
        owner = self.config.always_human.owner_user
        row = self.store.get_message(mid) if mid else None
        if not owner or event.get("user") != owner or not row:
            return
        emoji, state = event.get("reaction"), row["state"]
        if emoji in self.config.owner_signals.reactions_done and state in ("new", "assigned", "acked", "human_pending"):
            self.store.set_state(mid, "done", note="owner_reaction:" + emoji)
        elif emoji in self.config.owner_signals.reactions_ack and state == "assigned":
            self.store.set_state(mid, "acked", note="owner_reaction:" + emoji)

    def record_context(self, event):
        with self.lock:
            result = self.store.record_event(event)
            if result.created:
                self.store.set_route(result.message_id, None, "context:root", state="done")
                self.store.set_state(result.message_id, "done", note="context")
                self.counters["hydrated"] += 1
            return result

    def _hydrate(self, event):
        source = self.reconcile_source
        thread = event.get("thread_ts")
        if source is None or not thread or thread == event.get("ts"):
            return
        if event["channel"] in self.config.excluded_channels:
            return
        if self.store.get_message(f"slack:{event['channel']}:{thread}"):
            return
        if (event["channel"], thread) in source.pending_roots:
            return
        try:
            root = source.hydrate_root(event["channel"], thread)
            if root:
                self.record_context(root)
        except Exception:
            self.failure("hydrate_root")

    def _human_notice(self, mid, kind="message", attention=None):
        row = self.store.get_message(mid)
        if row.get("is_bot") and self.config.bot_messages.policy != "route_all":
            return
        if not adapter_entries(self.config, self.notifiers, "human_queue") and not self._missing_human_logged:
            LOG.warning("No human queue notifier configured")
            self._missing_human_logged = True
        self.human.offer(mid, "attention:" + attention if attention else kind,
                         immediate=attention in self.config.human_queue.immediate_flags)

    def flush(self, now):
        with self.lock:
            self.human.flush(now)
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
