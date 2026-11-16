"""Service orchestration with transient routing text and metadata-only delivery."""
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from collections import Counter
import logging
from pathlib import Path
import threading
import sys
import time
from urllib.parse import quote

from slack_dispatch.config.schema import FOLLOWUP_POLICIES
from slack_dispatch.ingest.membership import NOT_MEMBER
from slack_dispatch.ingest.user_directory import carries_app_evidence, is_bot_message
from slack_dispatch.notify.wake import Deliver
from slack_dispatch.notify import effective_lane, notify_human, wake_switch_path
from slack_dispatch.notify.dispatch_card import card_wanted
from slack_dispatch.notify.session_resolve import SessionResolver
from slack_dispatch.notify.base import DeliveryRecorder, error_detail
from slack_dispatch.notify.session_resolve import AMBIGUOUS
from slack_dispatch.queue.human import export_jsonl
from slack_dispatch.resolver.owner import VERSION as OWNER_VERSION, OwnerResolver, suggestion_for
from slack_dispatch.router.apply import apply_decision
from slack_dispatch.router.classifier import Suggestion
from slack_dispatch.router.engine import route

LOG = logging.getLogger(__name__)
HUMAN_QUEUE = "human_queue"


def console_base_url(config):
    """The operator console base URL without a trailing slash, or None when unset (P10)."""
    base = getattr(getattr(config, "console", None), "base_url", None)
    return base.rstrip("/") if isinstance(base, str) and base.strip() else None


def adapter_entries(config, notifiers, purpose):
    """The existing builder returns adapters in configuration order."""
    return [(index, spec, notifier)
            for index, (spec, notifier) in enumerate(zip(config.notify_adapters,
                                                       notifiers, strict=True))
            if spec.purpose == purpose]


def session_resolvers(notifiers):
    """Distinct session resolvers shared by builder-created session adapters."""
    found = {}
    for notifier in notifiers:
        resolver = getattr(notifier, "session_resolver", None)
        if isinstance(resolver, SessionResolver):
            found[id(resolver)] = resolver
    return list(found.values())


def can_notify(config, store, spec, lane, notifier=None, message_ids=None):
    """Gate one delivery; False means not delivered, so callers keep receipts unwritten.

    Human-queue adapters address the operator target from their template: no lane
    lookup, session resolution, pause check or global wake switch applies to them.
    Lane session adapters use the same precedence as the builder (effective_lane). When
    the notifier carries a session resolver, an id that is not live or is ambiguous also
    blocks the delivery here, so it is retried later instead of counted as delivered.
    message_ids lets the resolver count distinct skipped (lane, id) pairs.
    """
    if spec.purpose == HUMAN_QUEUE:
        return True
    if spec.purpose == "lane" and spec.kind != "file_inbox":
        if not config.wake_policy.enabled or wake_switch_path(config).exists():
            return False
    if spec.kind != "session_message":
        return True
    session, paused = effective_lane(config, store, lane)
    if paused or not session:
        return False
    resolver = getattr(notifier, "session_resolver", None)
    if isinstance(resolver, SessionResolver):
        return resolver.resolve_target(lane, session, warn_duplicates=False,
                                       message_ids=message_ids) is not None
    return True


# P18 delivery log bookkeeping: one delivery_log row per attempt to reach a session or the
# assistant DM. file_inbox adapters are never logged (their records are per message).

DELIVERY_PRUNE_SECONDS = 86400
# P20: directory name lookups per flush (the rest wait for the next flush) and the recheck
# interval used when the directory has no ttl_seconds attribute.
NAME_LOOKUPS_PER_FLUSH = 20
DIRECTORY_TTL_FALLBACK = 86400
# fix1: total time one flush may spend on name lookups (the rest wait for the next flush) and
# the largest number of users kept in the name queue and in the checked map (oldest dropped).
NAME_LOOKUP_BUDGET_SECONDS = 3.0
NAME_TRACK_LIMIT = 5000


# P22: item states in which the lane had already read (acked) or closed (done) an item, so a
# text edit under wake_policy.followup `changed_after_ack` is worth a followup wake.
FOLLOWUP_READ_STATES = ("acked", "done")
# P22: Pipeline._edit_verdict outcomes.
EDIT_IGNORE, EDIT_FIRST, EDIT_WAKE = "ignore", "first", "wake"


# P23: default wake_policy.catchup_minutes (0 disables catch-up wakes).
CATCHUP_MINUTES_DEFAULT = 30


def catchup_minutes(config):
    """wake_policy.catchup_minutes (P23); configs without a valid value use 30."""
    value = getattr(getattr(config, "wake_policy", None), "catchup_minutes", CATCHUP_MINUTES_DEFAULT)
    return value if type(value) is int and value >= 0 else CATCHUP_MINUTES_DEFAULT


def catchup_recent(config, ts, now):
    """P23: whether a Slack ts lies within wake_policy.catchup_minutes of `now` (inclusive).

    The distance is absolute, so a ts slightly ahead of the service clock (clock skew) also
    counts. An unreadable ts or a window of 0 is never recent.
    """
    window = catchup_minutes(config) * 60
    if not window:
        return False
    try:
        return abs(Decimal(str(now)) - Decimal(str(ts))) <= window
    except Exception:
        return False


def followup_policy(config):
    """wake_policy.followup (P22); configs without the key use `changed_after_ack`."""
    value = getattr(getattr(config, "wake_policy", None), "followup", None)
    return value if value in FOLLOWUP_POLICIES else FOLLOWUP_POLICIES[0]


def edit_text_changed(previous_body, text):
    """P22: whether an edit changed the text: the normalized new text against the stored body
    before the update, both whitespace-trimmed. A missing stored body (or a missing new text)
    counts as changed, since equality cannot be shown."""
    if not isinstance(previous_body, str) or not isinstance(text, str):
        return True
    return text.strip() != previous_body.strip()


def _trim_oldest(entries, limit):
    """Drop the oldest keys of an insertion-ordered dict until it holds `limit` (P20 fix1)."""
    while len(entries) > limit:
        entries.pop(next(iter(entries)))


def skip_reason(config, store, spec, lane, notifier=None):
    """The delivery_log status of an attempt `can_notify` refused (P18).

    Call it right after `can_notify` returned False: the ambiguous case reads the state the
    shared resolver recorded during that call.
    """
    if spec.purpose == "lane" and spec.kind != "file_inbox" and (
            not config.wake_policy.enabled or wake_switch_path(config).exists()):
        return "skipped_paused"
    session, paused = effective_lane(config, store, lane)
    if paused:
        return "skipped_paused"
    resolver = getattr(notifier, "session_resolver", None)
    if session and isinstance(resolver, SessionResolver) and resolver.lane_state.get(lane) == AMBIGUOUS:
        return "skipped_ambiguous"
    return "skipped_no_session"


def lane_kind(store, ids, catchup=()):
    """Delivery log kind of a lane wake: `reroute`, `new`, `backfill` or `catchup` (P18).

    P23: a reconcile item whose id is in `catchup` (a recent backfill offered to the wake
    engine) counts as `catchup`; a wake of reconcile items only is `catchup` when any of
    them is one, else `backfill`.
    """
    kinds = set()
    for mid in ids:
        row = store.get_message(mid) or {}
        if str(row.get("route_reason") or "").startswith("reroute:"):
            kinds.add("reroute")
        elif row.get("origin") == "reconcile":
            kinds.add("catchup" if mid in catchup else "backfill")
        else:
            kinds.add("new")
    if "reroute" in kinds:
        return "reroute"
    if kinds and kinds <= {"backfill", "catchup"}:
        return "catchup" if "catchup" in kinds else "backfill"
    return "new"


def human_kind(spec, rows, digest):
    """Delivery log kind of a human notice: `card`, `digest`, `attention`, `reminder` or `new`."""
    if spec.kind == "dispatch_card":
        return "card"
    if digest:
        return "digest"
    kinds = {row["kind"] for row in rows}
    if any(kind.startswith("attention:") or kind == "storm" for kind in kinds):
        return "attention"
    if kinds & {"unassigned", "reassign"}:
        return "reminder"
    return "new"


def build_recorders(config, store, notifiers, clock):
    """One DeliveryRecorder per non-file_inbox adapter position (P18), empty when disabled.

    Adapters that log their own attempts (`logs_deliveries = True`) get the recorder as
    `delivery_recorder`, so timer reminders through the same adapter objects are logged too.
    """
    settings = getattr(config, "delivery_log", None)
    if settings is None or not settings.enabled or not callable(getattr(store, "add_delivery", None)):
        return {}
    recorders = {}
    for index, (spec, notifier) in enumerate(zip(config.notify_adapters, notifiers)):
        if spec.kind == "file_inbox":
            continue
        recorder = DeliveryRecorder(store, purpose=spec.purpose, adapter=spec.kind, clock=clock,
                                    delivered_patterns=settings.delivered_patterns,
                                    not_delivered_patterns=settings.not_delivered_patterns)
        recorders[index] = recorder
        if getattr(notifier, "logs_deliveries", False) is True:
            notifier.delivery_recorder = recorder
    return recorders


def logged_call(recorder, notifier, lane, kind, ids, call):
    """Run one adapter call inside a delivery_log attempt (P18) and re-raise its errors.

    Self-logging adapters write their own row (with the relay result); for the others the
    row is written here: `sent`, `dry_run` for the service dry-run adapter, or `failed`.
    """
    if recorder is None:
        return call()
    own = getattr(notifier, "logs_deliveries", False) is True
    with recorder.attempt(kind, ids):
        try:
            result = call()
        except Exception as error:
            if not own:
                recorder.record(lane, ids, "failed", kind=kind, detail=error_detail(error))
            raise
    if not own:
        dry = getattr(notifier, "service_dry_run", False) is True
        recorder.record(lane, ids, "dry_run" if dry else "sent", kind=kind)
    return result


def queue_human_notice(config, store, mid, kind="message", *, immediate=False, adapter=None, now):
    """Queue one durable human-queue notice row per human_queue adapter (no delivery).

    The service's flush delivers queued rows, so an offline caller (the P16 `repair
    bot-flag` command) can queue a notice the running service sends. Adapter positions are
    configuration positions, as the builder creates notifiers in configuration order.
    """
    with store._transaction() as db:
        for index, spec in enumerate(config.notify_adapters):
            if spec.purpose != HUMAN_QUEUE or (adapter is not None and index != adapter):
                continue
            if spec.kind == "dispatch_card" and not card_wanted(
                    spec, kind, immediate, config.human_queue.mode,
                    (store.get_message(mid) or {}).get("state")):
                continue  # P12: cards follow immediate notices of human_pending items only
            db.execute("INSERT OR IGNORE INTO human_notices "
                       "(message_id, kind, adapter, queued_at, immediate) VALUES (?, ?, ?, ?, ?)",
                       (mid, kind, index, now, immediate))


class HumanQueueDelivery:
    """Durable ids-only digest backlog and per-adapter one-shot receipts."""
    def __init__(self, config, store, notifiers, clock):
        self.config, self.store, self.notifiers, self.clock = config, store, notifiers, clock
        self.failures = 0
        self.delivered = 0
        self.recorders = {}  # P18: adapter position -> DeliveryRecorder (set by the pipeline)

    def offer(self, mid, kind="message", *, immediate=False, adapter=None, now=None):
        now = self.clock() if now is None else now
        queue_human_notice(self.config, self.store, mid, kind, immediate=immediate,
                           adapter=adapter, now=now)
        if immediate or self.config.human_queue.mode == "immediate":
            return self.flush(now, immediate_only=True)
        return 0

    def _send(self, index, rows, now, digest):
        spec, notifier = self.config.notify_adapters[index], self.notifiers[index]
        if digest and spec.kind == "dispatch_card":
            return 0  # P12: digest notices never produce cards
        if not can_notify(self.config, self.store, spec, HUMAN_QUEUE, notifier):
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
        # The last line lists lane ids for `assign ID LANE`; the human queue pseudo-lane is excluded and an empty list omits the line.
        lane_ids = [lane.id for lane in self.config.lanes if lane.id != HUMAN_QUEUE]
        lanes = "Lane ids for assign: " + ", ".join(lane_ids) if lane_ids else ""
        summary = (f"{title}: {len(messages)} item(s); channels: "
                   + ", ".join(f"{channel}={count}" for channel, count in counts.items())
                   + "; ids: " + ", ".join(ids) + ". " + "; ".join(assigned)
                   + ("; timers: " + timer_summary if timer_summary else "")
                   + "\n" + commands + ("\n" + lanes if lanes else ""))
        base_url = console_base_url(self.config)
        if base_url and digest:
            # P10: with a console, digests carry per-channel counts and the console link only.
            summary = (f"{title}: {len(messages)} item(s); channels: "
                       + ", ".join(f"{channel}={count}" for channel, count in counts.items())
                       + (". " + "; ".join(assigned) if assigned else "")
                       + ("; timers: " + timer_summary if timer_summary else "")
                       + "\nOpen: " + base_url + "/")
        elif base_url:
            # Immediate notices keep their text and end with a link to the message.
            target = "#msg=" + quote(ids[0], safe=":._-") if len(ids) == 1 else ""
            summary += "\nOpen: " + base_url + "/" + target
        metadata = dict(count=len(messages), channels=counts, ids=ids, summary=summary,
                        timer_counts=timer_counts,
                        timers=sorted({row["kind"] for row in rows
                                       if row["kind"] in ("unassigned", "reassign")}))
        try:
            recorder, kind, covered = self.recorders.get(index), human_kind(spec, rows, digest), sorted(messages)
            if spec.dry_run:
                LOG.info("Dry-run human notice: %s", summary)
                if recorder is not None:
                    recorder.record(HUMAN_QUEUE, covered, "dry_run", kind=kind)
                return 0
            logged_call(recorder, notifier, HUMAN_QUEUE, kind, covered,
                        lambda: notify_human(notifier, ids, metadata))
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
        for index, spec, _ in adapter_entries(self.config, self.notifiers, "human_queue"):
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
                if spec.kind == "dispatch_card":
                    # P12: a queued card is dropped once its message left the human queue.
                    valid = valid and message["state"] == "human_pending"
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


def routing_config(config, store):
    """`config` with the store's active channel exclusions added to `excluded_channels` (P15)."""
    lookup = getattr(store, "channel_exclusion_ids", None)
    extra = lookup() if callable(lookup) else ()
    added = sorted(set(extra) - set(config.excluded_channels))
    if not added:
        return config
    return replace(config, excluded_channels=tuple(config.excluded_channels) + tuple(added))


def build_owner_resolver(config):
    """The P17 owner resolver when `owner_resolver.enabled`, else None."""
    settings = getattr(config, "owner_resolver", None)
    return OwnerResolver(config) if settings is not None and settings.enabled else None


class OwnerFirstSuggester:
    """P17: the owner resolver suggests first; the wrapped classifier fills only rows the
    resolver left without a lane. Suggestions never assign a lane (classifier contract)."""
    enabled = True

    def __init__(self, resolver, store, inner):
        self.resolver, self.store, self.inner = resolver, store, inner
        self.ran = False

    def suggest(self, message):
        self.ran = True
        found = owner_suggestion(self.resolver, self.store, message)
        if found is not None:
            return Suggestion(found[0], found[1], OWNER_VERSION)
        if self.inner is None or not getattr(self.inner, "enabled", True):
            return None
        return self.inner.suggest(message)


def owner_suggestion(resolver, store, message):
    """(lane, reason) from the resolver, or None; a resolver failure is logged by class only."""
    try:
        return suggestion_for(resolver, store, message)
    except Exception as error:
        LOG.warning("Owner resolver skipped class=%s", type(error).__name__)
        return None


@dataclass(frozen=True)
class HandleResult:
    message_id: str | None
    kind: str
    duplicate: bool = False
    state: str | None = None
    lane: str | None = None
    backfill: bool = False
    # P23: a recent reconcile message offered to the wake engine like a live one.
    catchup: bool = False


class Pipeline:
    def __init__(self, config, store, suggester, notifiers, wake_engine, clock):
        self.config, self.store, self.suggester = config, store, suggester
        self.notifiers = list(notifiers)
        self.wake_engine, self.clock = wake_engine, clock
        self.lock = threading.RLock()
        self.counters = dict(events=0, routed=0, human_pending=0, delivered=0, failures=0,
                             duplicates=0, prehandled=0, hydrated=0)
        # P22: edits recorded without a wake and edits that went on to a wake offer; both
        # appear in the heartbeat once either is nonzero.
        self.edits_ignored = 0
        self.edits_woken = 0
        # P22: message id -> clock time of the last followup wake offered for an edit
        # (memory only, oldest dropped past NAME_TRACK_LIMIT).
        self._edit_wakes = {}
        # P23: recent reconcile items offered to the wake engine (`catchup_woken`) and
        # reconcile items assigned to a lane without a wake offer (`backfill_silent`); both
        # appear in the heartbeat once either is nonzero.
        self.catchup_woken = 0
        self.backfill_silent = 0
        # P23: ids of catch-up offers whose lane wake was not attempted yet, so the delivery
        # log row says `catchup` (memory only, oldest dropped past NAME_TRACK_LIMIT).
        self.catchup_ids = {}
        self.reconcile_source = None
        # P15: optional MembershipCache; the service sets it when membership is enabled.
        self.membership = None
        self.excluded_not_member = 0
        # P16: optional UserDirectory; the service sets it when user_directory is enabled.
        self.directory = None
        self.app_posts_human = 0
        self.bot_posts = 0
        # P20: authors waiting for a directory name lookup (resolved from flush, off the
        # event path) and when each author was last checked (epoch seconds, memory only).
        self._name_queue = {}
        self._names_checked = {}
        self.directory_names = 0
        # fix1: the lookup time budget clock (monotonic; tests replace it).
        self.name_clock = time.monotonic
        self.last_event_at = None
        self._missing_human_logged = False
        self._retry = {}
        # P21 fix1: ids a Wake now request offered while acked; the flush lets them through
        # once (see _current_ids) and drops them after the wake was sent.
        self.wake_now_ids = set()
        self._export_pending = False
        self.human = HumanQueueDelivery(config, store, self.notifiers, clock)
        # P17: optional owner resolver (owner_resolver.enabled).
        self.owner_resolver = build_owner_resolver(config)
        self.owner_reply_closed = 0
        # P18: delivery log recorders, shared with the human queue and the adapters.
        self.recorders = build_recorders(config, store, self.notifiers, clock)
        self.human.recorders = self.recorders
        self._pruned_at = None
        self.prune_deliveries(count_failure=False)
        # Keep the package's v1 migration compatible. This table is service-owned.
        with store._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS service_deliveries (
                source TEXT NOT NULL, channel TEXT NOT NULL, thread_ts TEXT NOT NULL,
                lane TEXT NOT NULL, delivered_at REAL NOT NULL,
                PRIMARY KEY (source, channel, thread_ts, lane))""")

    def prune_deliveries(self, now=None, *, count_failure=True):
        """Delete delivery_log rows older than delivery_log.keep_days (P18); returns the count.

        Runs at construction (service start) and from flush once a day.
        """
        settings = getattr(self.config, "delivery_log", None)
        prune = getattr(self.store, "prune_deliveries", None)
        if settings is None or not callable(prune):
            return 0
        try:
            now = float(self.clock() if now is None else now)
            self._pruned_at = now
            cutoff = datetime.fromtimestamp(max(0.0, now - settings.keep_days * 86400), timezone.utc)
            return prune(cutoff.isoformat(timespec="microseconds"))
        except Exception as error:
            if count_failure:
                self.failure("delivery_prune")
            else:
                LOG.error("Pipeline exception operation=delivery_prune class=%s", type(error).__name__)
            return 0

    def failure(self, operation):
        with self.lock:
            self.counters["failures"] += 1
            error = sys.exc_info()[1]
            LOG.error("Pipeline exception operation=%s class=%s", operation,
                      type(error).__name__ if error else "Unknown")

    def snapshot(self):
        with self.lock:
            result = dict(self.counters, failures=self.counters["failures"] + self.human.failures,
                          delivered=self.counters["delivered"] + self.human.delivered)
            resolvers = session_resolvers(self.notifiers)
            if resolvers:
                # Session wakes skipped because the configured id was not live.
                result["wake_skipped_no_session"] = sum(
                    resolver.skipped_no_session for resolver in resolvers)
            if self.membership is not None:
                result.update(self.membership.snapshot(), excluded_not_member=self.excluded_not_member)
            if self.directory is not None:
                # P16 counters appear with the directory, like the P15 membership counters.
                result.update(self.directory.snapshot(), app_posts_human=self.app_posts_human,
                              bot_posts=self.bot_posts, directory_names=self.directory_names)
            if self.owner_resolver is not None:
                # P17: human queue items closed by an owner reply in their thread.
                result["owner_reply_closed"] = self.owner_reply_closed
            if self.edits_ignored or self.edits_woken:
                result.update(edits_ignored=self.edits_ignored, edits_woken=self.edits_woken)
            if self.catchup_woken or self.backfill_silent:
                result.update(catchup_woken=self.catchup_woken, backfill_silent=self.backfill_silent)
            return result

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
        recorder = self.recorders.get(index)
        if not can_notify(self.config, self.store, spec, lane, notifier, list(ids)):
            if recorder is not None:
                try:
                    recorder.record(lane, list(ids),
                                    skip_reason(self.config, self.store, spec, lane, notifier),
                                    kind=lane_kind(self.store, ids, self.catchup_ids))
                except Exception as error:
                    LOG.error("Delivery log write failed class=%s", type(error).__name__)
            return False
        kind = None
        if recorder is not None:
            try:
                kind = lane_kind(self.store, ids, self.catchup_ids)
            except Exception as error:
                LOG.error("Delivery log write failed class=%s", type(error).__name__)
                kind = "new"
        try:
            logged_call(recorder, notifier, lane, kind,
                        list(ids), lambda: notifier.notify(lane, list(ids)))
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
        if purpose == "lane" and inbox is not True:
            self._forget_catchup(ids)
        return sent

    def _forget_catchup(self, ids):
        """P23: drop catch-up marks once their lane wake was attempted, except ids still
        waiting in a retry queue (their retry is logged as `catchup` too)."""
        if not self.catchup_ids:
            return
        waiting = set().union(*self._retry.values()) if self._retry else set()
        for mid in ids:
            if mid not in waiting:
                self.catchup_ids.pop(mid, None)

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
                result = self._handle(event)
            except Exception:
                self.failure("event")
                # Reconcile must not advance its cursor after a failed callback.
                raise RuntimeError("Event processing failed") from None
            self._note_author(event, result)
            return result

    def resolve_names_enabled(self):
        settings = getattr(self.config, "user_directory", None)
        return self.directory is not None and bool(getattr(settings, "resolve_names", True))

    def _note_author(self, event, result):
        """P20: queue a new message author for a directory name lookup (done in flush).

        Every author counts, not only app posts. An author checked within the directory
        TTL is not queued again. Never raises: names are display data only.
        """
        try:
            user = event.get("user")
            if (not user or not self.resolve_names_enabled() or result.duplicate
                    or result.kind != "message" or event.get("subtype") == "message_deleted"):
                return
            checked = self._names_checked.get(user)
            ttl = getattr(self.directory, "ttl_seconds", DIRECTORY_TTL_FALLBACK)
            now = self.clock()
            if checked is not None and 0 <= now - checked < ttl:
                return
            if user not in self._name_queue:
                self._name_queue[user] = None
                _trim_oldest(self._name_queue, NAME_TRACK_LIMIT)
        except Exception as error:
            LOG.error("Directory name queue failed class=%s", type(error).__name__)

    def resolve_names(self, now=None, *, limit=NAME_LOOKUPS_PER_FLUSH,
                      budget=NAME_LOOKUP_BUDGET_SECONDS):
        """P20: look up queued authors through the directory, outside the pipeline lock.

        Under the lock only the next `limit` user ids are taken from the queue; the
        users.info calls run with the lock released (the directory stores each row through
        the ledger's own transaction), and the bookkeeping takes the lock again. Lookups
        stop once `budget` seconds (`name_clock`, monotonic) have passed; the users not
        reached go back to the front of the queue for the next flush. Same TTL, cooldown and
        fail-open rules as the bot flag; a failed lookup leaves the author unchecked so a
        later message queues it again after the cooldown.
        """
        with self.lock:
            if not self._name_queue or not self.resolve_names_enabled():
                return 0
            directory = self.directory
            batch = list(self._name_queue)[:limit]
            for user in batch:
                self._name_queue.pop(user, None)
        now = self.clock() if now is None else now
        started = self.name_clock()
        done = 0
        for position, user in enumerate(batch):
            if position and self.name_clock() - started >= budget:
                with self.lock:
                    rest = batch[position:]
                    self._name_queue = dict.fromkeys(rest + [name for name in self._name_queue
                                                            if name not in rest])
                    _trim_oldest(self._name_queue, NAME_TRACK_LIMIT)
                break
            before = getattr(directory, "names", 0)
            try:
                row = directory.ensure_name(user)
            except Exception as error:
                LOG.error("Directory name lookup failed class=%s", type(error).__name__)
                continue
            with self.lock:
                self.directory_names += max(0, getattr(directory, "names", 0) - before)
                if row is not None:
                    self._names_checked.pop(user, None)
                    self._names_checked[user] = now
                    _trim_oldest(self._names_checked, NAME_TRACK_LIMIT)
            done += 1
        return done

    def is_bot(self, event):
        """The one P16 bot decision for socket and reconcile events (see user_directory).

        A caller-supplied `is_bot: True` is kept. The directory (users.info) is consulted
        only for a message posted through an app (`bot_id` or `app_id`), so a human posting
        by hand never costs a lookup; a failed lookup counts the author as human.
        """
        if event.get("is_bot") is True:
            return True
        directory = None
        if (self.directory is not None and event.get("event_type") == "message"
                and event.get("subtype") != "message_deleted" and carries_app_evidence(event)):
            directory = self.directory
        return is_bot_message(event, self.config, directory)

    def _handle(self, event):
        event = dict(event, is_bot=self.is_bot(event))
        recorded = self.store.record_event(event)
        mid = recorded.message_id
        if recorded.duplicate:
            self.counters["duplicates"] += 1
        elif recorded.kind == "message" and event.get("subtype") != "message_deleted":
            if event["is_bot"]:
                self.bot_posts += 1
            elif carries_app_evidence(event):
                self.app_posts_human += 1
        if not recorded.duplicate and recorded.kind == "reaction_added":
            self._owner_reaction(event, mid)
        if recorded.duplicate or recorded.kind != "message":
            return HandleResult(mid, recorded.kind, recorded.duplicate)
        message = self.store.get_message(mid) if mid else None
        if message is None or event.get("subtype") == "message_deleted":
            return HandleResult(mid, recorded.kind)
        if self.membership is not None and self.membership.excludes(
                event.get("channel"), event.get("channel_type") or message.get("channel_type")):
            # P15 member-only gate: recorded, never routed, queued or counted as human_pending.
            self.store.set_state(mid, "excluded", note=NOT_MEMBER)
            self.excluded_not_member += int(event.get("channel") not in self.config.excluded_channels)
            return HandleResult(mid, recorded.kind, state="excluded")
        verdict = (self._edit_verdict(mid, message, recorded, event)
                   if event.get("subtype") == "message_changed" else None)
        if verdict == EDIT_IGNORE:
            # P22: the ledger already holds the edit (body, edited_at); no routing, inbox
            # record, wake offer or human notice, and the item keeps its state and lane.
            self._edit_ignored(mid)
            return HandleResult(mid, recorded.kind, state=message.get("state"),
                                lane=message.get("lane"))
        # P22: only edit wakes count in the edit counters and the edit wake window; an edit
        # routed as a first assignment (EDIT_FIRST) does not.
        edit = verdict == EDIT_WAKE
        # P22 fix1: a human_pending item re-routed by a text edit.
        requeued = verdict == EDIT_FIRST and message.get("state") == "human_pending"
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
            if note == "owner_post":
                self._close_on_owner_reply(message)
            return HandleResult(mid, recorded.kind, state="done", lane=message.get("lane"))
        if "text" in event:
            message["text"] = event["text"]
        message["is_bot"] = event["is_bot"]
        decision = route(message, self.routing_config(), self.store.lookup_binding)
        suggester = self.suggester
        if self.owner_resolver is not None:
            suggester = OwnerFirstSuggester(self.owner_resolver, self.store, self.suggester)
        outcome = apply_decision(self.store, mid, decision, suggester)
        if (outcome.state == "human_pending" and self.owner_resolver is not None
                and not suggester.ran):
            # apply_decision asks a suggester only for rule `none`; other queued items
            # (for example always_human) still get the resolver's suggestion.
            found = owner_suggestion(self.owner_resolver, self.store, message)
            if found is not None:
                self.store.set_suggestion(mid, found[0], found[1], OWNER_VERSION)
        self.counters["routed"] += 1
        backfill = catchup = False
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
            catchup = (event.get("origin") == "reconcile"
                       and catchup_recent(self.config, event.get("ts"), self.clock()))
            if event.get("origin") == "reconcile" and not catchup:
                # Older backfill fills durable inboxes only: no session wake, no storm accounting.
                backfill = inboxed
                self.backfill_silent += 1
            else:
                if catchup:
                    # P23: a backfill item within wake_policy.catchup_minutes (a post made
                    # during a short outage) is offered like a live one; debounce and storm
                    # rules apply and the inbox record keeps kind `backfill`.
                    self.catchup_woken += 1
                    self.catchup_ids.pop(mid, None)
                    self.catchup_ids[mid] = None
                    _trim_oldest(self.catchup_ids, NAME_TRACK_LIMIT)
                wake = self.wake_engine.offer(lane, message, message.get("author"))
                if edit:
                    self.edits_woken += 1
                    self._edit_wakes.pop(mid, None)
                    self._edit_wakes[mid] = self.clock()
                    _trim_oldest(self._edit_wakes, NAME_TRACK_LIMIT)
                if isinstance(wake, Deliver):
                    self._send("lane", lane, [mid], inbox=False)
                if getattr(wake, "notice", None) is not None:
                    self._human_notice(mid, kind="storm")
                    self.wake_engine.notices.clear()
        elif outcome.state == "human_pending" and requeued:
            # P22 fix1: still queued after the edit: ledger (and queue export) only, no
            # second queue notice.
            self._edit_ignored(mid)
            self._export_queue()
        elif outcome.state == "human_pending":
            self.counters["human_pending"] += 1
            self._export_queue()
            self._human_notice(mid, attention=decision.attention)
        if outcome.state == "assigned" and decision.attention:
            self._human_notice(mid, attention=decision.attention)
        return HandleResult(mid, recorded.kind, state=outcome.state, lane=outcome.lane, backfill=backfill,
                            catchup=catchup)

    def _edit_ignored(self, mid):
        self.edits_ignored += 1
        LOG.info("Edit recorded without wake id=%s", mid)

    def _edit_verdict(self, mid, message, recorded, event):
        """P22: what a message_changed event of an existing row does, per wake_policy.followup.

        EDIT_FIRST: ordinary routing as a first assignment (inbox record, wake and attention
        notice as usual, outside the edit counters and window): any edit of a `new` item
        (fix1: an item recorded but never routed), and under `changed_after_ack` a text edit
        of a `human_pending` item. EDIT_WAKE: routing, followup inbox record and wake offer
        as before P22: every other edit under `always`, and under `changed_after_ack` a text
        edit of an item that was `acked` or `done` before this event (record_event leaves
        the state alone) with no edit wake offered for the same id within
        service.followup_window_seconds (inclusive, like replies). EDIT_IGNORE otherwise.
        """
        state = message.get("state")
        if state == "new":
            return EDIT_FIRST
        policy = followup_policy(self.config)
        if policy != "changed_after_ack":
            return EDIT_WAKE if policy == "always" else EDIT_IGNORE
        if not edit_text_changed(getattr(recorded, "previous_body", None), event.get("text")):
            return EDIT_IGNORE
        if state == "human_pending":
            return EDIT_FIRST
        if state not in FOLLOWUP_READ_STATES:
            return EDIT_IGNORE
        last = self._edit_wakes.get(mid)
        if last is not None and 0 <= self.clock() - last <= self.config.service.followup_window_seconds:
            return EDIT_IGNORE
        return EDIT_WAKE

    def _close_on_owner_reply(self, message):
        """P17: an owner reply in a thread closes the thread's earlier human queue items.

        Only `human_pending` items of the same thread with a lower ts become `done` with
        note `owner_reply`; their unsent human notices are dropped so a pending digest no
        longer lists them, and the queue export is rewritten. Lane items are untouched
        (the lane inbox shows `owner replied at`). Needs `owner_resolver.enabled` and
        `owner_resolver.close_queue_on_owner_reply`.
        """
        settings = getattr(self.config, "owner_resolver", None)
        root, ts = message.get("thread_ts"), message.get("ts")
        if (self.owner_resolver is None or not settings.close_queue_on_owner_reply
                or not root or root == ts):
            return 0
        lookup = getattr(self.store, "pending_in_thread", None)
        if not callable(lookup):
            return 0
        try:
            owner_ts = Decimal(str(ts))
            closed = [row["id"] for row in lookup(message["source"], message["channel"], root)
                      if Decimal(str(row["ts"])) < owner_ts]
        except Exception:
            self.failure("owner_reply_close")
            return 0
        for mid in closed:
            self.store.set_state(mid, "done", note="owner_reply")
        if closed:
            with self.store._transaction() as db:
                for mid in closed:
                    db.execute("DELETE FROM human_notices WHERE message_id=? AND sent_at IS NULL", (mid,))
            self.owner_reply_closed += len(closed)
            self._export_queue()
        return len(closed)

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
        if event["channel"] in self.routing_config().excluded_channels:
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

    def routing_config(self):
        """Config for routing: config `excluded_channels` plus the ledger channel exclusions.

        P15 console `Exclude channel` rows are read at runtime, so `route` treats them exactly
        like `excluded_channels` without a YAML edit. Stores without the lookup add nothing.
        """
        return routing_config(self.config, self.store)

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
            if self._pruned_at is None or not 0 <= now - self._pruned_at < DELIVERY_PRUNE_SECONDS:
                self.prune_deliveries(now)
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
                if spec.purpose == "lane" and spec.kind != "file_inbox":
                    self._forget_catchup(ids)
            if self._export_pending:
                self._export_queue()
            for lane, batch in self.wake_engine.flush(now):
                ids = self._current_ids(lane, batch, "lane")
                if ids:
                    self._send("lane", lane, ids, inbox=False)
                self.wake_now_ids.difference_update(batch)
        # P20 fix1: directory name lookups run after the pipeline lock is released, so a slow
        # users.info call never holds up handle_event.
        self.resolve_names(now)

    def _current_ids(self, lane, ids, purpose):
        """Ids still deliverable: human queue ids, and lane ids still assigned to the lane.
        P21 fix1: an id in `wake_now_ids` (a pending Wake now) also passes while acked."""
        result = []
        for mid in sorted(ids):
            row = self.store.get_message(mid)
            if row and (purpose == "human_queue" or
                        (row.get("lane") == lane and (row["state"] == "assigned" or (
                            row["state"] == "acked" and mid in self.wake_now_ids)))):
                result.append(mid)
        return result
