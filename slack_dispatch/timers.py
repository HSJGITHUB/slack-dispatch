"""Metadata-only overdue notices with durable per-adapter receipts."""
from copy import copy
from datetime import datetime
import logging
import shlex
import threading

from slack_dispatch.notify.file_drop import FileDropNotifier
from slack_dispatch.notify.session_message import SessionMessageNotifier
from slack_dispatch.pipeline import HUMAN_QUEUE, HumanQueueDelivery, adapter_entries, can_notify

LOG = logging.getLogger(__name__)


def _reminder_notifier(notifier, timer):
    """Label timer notifications without changing shared adapter templates."""
    label = "reminder: reassignment proposal" if timer == "reassign" else "reminder"
    if isinstance(notifier, SessionMessageNotifier):
        reminder = copy(notifier)
        parts = notifier.command_template
        if isinstance(parts, str):
            parts = shlex.split(parts)
        reminder.command_template = [
            part.replace("{message}", label + ": {message}") for part in parts]
        return reminder
    if isinstance(notifier, FileDropNotifier):
        reminder = copy(notifier)
        reminder.template = dict(notifier.template, notice=label)
        return reminder
    return notifier


class TimerSweep:
    def __init__(self, config, store, notifiers, clock, *, human=None):
        self.config, self.store = config, store
        self.notifiers, self.clock = list(notifiers), clock
        self.lock = threading.RLock()
        self.failures = 0
        self.human = human or HumanQueueDelivery(config, store, self.notifiers, clock)
        self._missing_human_logged = False
        with store._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS timer_events (
                message_id TEXT NOT NULL REFERENCES messages(id),
                timer TEXT NOT NULL, adapter INTEGER NOT NULL, sent_at REAL NOT NULL,
                PRIMARY KEY (message_id, timer, adapter))""")

    def _notice(self, row, timer, purpose, lane, now):
        if purpose == "human_queue" and row.get("is_bot") and self.config.bot_messages.policy != "route_all":
            return 0
        entries = adapter_entries(self.config, self.notifiers, purpose)
        if timer in ("unacknowledged", "reassign"):
            entries = [entry for entry in entries if entry[1].kind != "file_inbox"]
        if purpose == "human_queue" and not entries and not self._missing_human_logged:
            LOG.warning("No human queue notifier configured for timers")
            self._missing_human_logged = True
        sent = 0
        for index, spec, notifier in entries:
            if self.store._rows("SELECT 1 FROM timer_events "
                                "WHERE message_id=? AND timer=? AND adapter=?",
                                (row["id"], timer, index)):
                continue
            if purpose == "human_queue" and self.config.human_queue.mode == "digest":
                self.human.offer(row["id"], timer, adapter=index, now=now)
                continue
            if not can_notify(self.config, self.store, spec, lane, notifier, [row["id"]]):
                continue
            try:
                if spec.dry_run:
                    LOG.info("Dry-run timer notice: timer=%s id=%s", timer, row["id"])
                    continue
                if timer in ("unacknowledged", "reassign"):
                    notifier = _reminder_notifier(notifier, timer)
                notifier.notify(lane, [row["id"]])
                if getattr(notifier, "service_dry_run", False) is True:
                    continue
                with self.store._transaction() as db:
                    db.execute("INSERT OR IGNORE INTO timer_events VALUES (?, ?, ?, ?)",
                               (row["id"], timer, index, now))
                sent += 1
            except Exception:
                self.failures += 1
                LOG.error("Timer notification failed")
        return sent

    def run_once(self, now=None):
        now = self.clock() if now is None else now
        if isinstance(now, datetime):
            now = now.timestamp()
        sent = 0
        with self.lock:
            # The store has no offset or unlimited sentinel. Use SQLite's largest
            # signed LIMIT to read each relevant state once without truncation.
            rows = (row for state in ("assigned", "new", "human_pending")
                    for row in self.store.list_messages(state=state, limit=2**63 - 1))
            for row in rows:
                try:
                    state = row["state"]
                    if state in ("new", "human_pending"):
                        age = now - datetime.fromisoformat(row["received_at"]).timestamp()
                        if age >= self.config.timers.unassigned_seconds:
                            sent += self._notice(row, "unassigned", "human_queue", HUMAN_QUEUE, now)
                    elif state == "assigned" and not row.get("ack_at"):
                        stamp = row.get("assigned_at") or row["received_at"]
                        age = now - datetime.fromisoformat(stamp).timestamp()
                        if age >= self.config.timers.unacknowledged_seconds:
                            sent += self._notice(row, "unacknowledged", "lane", row["lane"], now)
                        if age >= self.config.timers.reassign_seconds:
                            sent += self._notice(row, "reassign", "human_queue", HUMAN_QUEUE, now)
                except Exception:
                    self.failures += 1
                    LOG.error("Timer item failed")
            sent += self.human.flush(now)
        return sent
