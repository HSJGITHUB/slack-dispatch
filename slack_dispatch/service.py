"""Resident orchestration for the existing intake, ledger and notification packages."""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import logging
import os
import queue
from pathlib import Path
import sys
import time

from slack_dispatch import __version__
from slack_dispatch.config.loader import ConfigError, load_config
from slack_dispatch.decisions import DecisionConsumer, card_dir
from slack_dispatch.ingest import build_sources
from slack_dispatch.ingest.membership import MembershipCache
from slack_dispatch.ingest.normalize import TIMESTAMP, message_body, safe
from slack_dispatch.ingest.slack_reconcile import SlackReconcileSource
from slack_dispatch.ingest.slack_socket import SlackSocketSource
from slack_dispatch.ingest.user_directory import UserDirectory
from slack_dispatch.ledger.store import WAKE_KIND_NOW, LedgerStore
from slack_dispatch.notify import DEFAULT_STATE_DIR, build_notifiers, wake_dirs_differ, wake_switch_path
from slack_dispatch.notify.wake import Batch, Defer, Deliver, WakePolicyEngine
from slack_dispatch.pipeline import Pipeline
from slack_dispatch.router.classifier import AnthropicSuggester, NullSuggester
from slack_dispatch.runtime import (
    AlreadyRunning, ControlFlags, HeartbeatWriter, SingleInstanceLock, configure_logging,
)
from slack_dispatch.timers import TimerSweep

LOG = logging.getLogger(__name__)
REHYDRATE_STATES = ("new", "assigned", "acked", "human_pending")
REHYDRATE_CALLS_PER_MINUTE = 40
WAKE_REQUEST_SECONDS = 5


class _DryRunNotifier:
    service_dry_run = True

    def notify(self, lane, message_ids):
        LOG.info("Dry-run notification: lane=%s count=%d ids=%s", lane,
                 len(message_ids), list(message_ids))


class _ServiceSocketSource(SlackSocketSource):
    """Service configuration uses the shared socket normalization path."""


class _ServiceReconcileSource(SlackReconcileSource):
    """Resident reconcile source. Since P16 it no longer decides the bot flag: shared
    normalization keeps `user`, `subtype` and `bot_id`, and the pipeline decides once."""
    # The resident loop must keep heartbeat, stop flag, flush and timers alive
    # while the first catch-up pass runs on the worker thread.
    wait_for_first_pass = False

    def __init__(self, *args, known_bot_users=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.known_bot_users = known_bot_users


class _OnceReconcileSource(_ServiceReconcileSource):
    """Reuse startup initialization but stop before the interval thread can poll."""
    wait_for_first_pass = True

    def catch_up(self):
        try:
            return super().catch_up()
        finally:
            self._stop.set()


def build_suggester(config):
    entry = config.classifier
    if not entry.enabled:
        return NullSuggester()
    taxonomy = {lane.id: entry.lane_descriptions.get(lane.id, lane.id)
                for lane in config.lanes}
    return AnthropicSuggester(entry.model, entry.api_key_env, taxonomy,
                             entry.taxonomy_version)


def build_service_sources(config, on_event, store, *, once=False):
    if once:
        config = replace(config, sources=tuple(entry for entry in config.sources
                                               if entry.kind == "slack_reconcile"))
    sources = build_sources(config, on_event, store)
    result = []
    for source in sources:
        # Source text is transient routing input. The ledger keeps its own policy.
        source.config = replace(source.config, store_body=True)
        if isinstance(source, SlackReconcileSource):
            cls = _OnceReconcileSource if once else _ServiceReconcileSource
            source = cls(source.config, source.on_event, source.env_file, source.user_token_key,
                         store, excluded_channels=config.excluded_channels,
                         known_bot_users=config.bot_messages.known_bot_users)
        elif isinstance(source, SlackSocketSource):
            source = _ServiceSocketSource(source.config, source.on_event,
                                           source.env_file, source.app_token_key)
        if isinstance(source, SlackReconcileSource):
            source.owner_user = config.always_human.owner_user
        result.append(source)
    return result


def build_membership(config, store, sources, clock):
    """The P15 membership cache, shared by the pipeline gate and the reconcile sources.

    None when `membership.enabled` is false or no reconcile source exists (its read-only
    client is the one the cache uses). The previous run's table is loaded at once; the
    first refresh runs at the start of the first reconcile pass.
    """
    entry = config.membership
    reconcilers = [source for source in sources if isinstance(source, SlackReconcileSource)]
    if not entry.enabled or not reconcilers:
        return None
    reconcile = reconcilers[0]
    configured = [channel for channel in config.channel_defaults
                  if channel not in config.excluded_channels]
    cache = MembershipCache(lambda: reconcile.client, store, entry.refresh_seconds,
                            policy=entry.policy, clock=clock, configured_channels=configured)
    for source in reconcilers:
        source.membership = cache
    cache.load()
    return cache


def build_user_directory(config, store, sources, clock):
    """The P16 user directory used by the pipeline's bot flag (users.info, cached in the
    ledger table `user_directory`).

    None when `user_directory.enabled` is false or no reconcile source exists (its
    read-only client is the one the directory uses); the rule then is a missing author,
    subtype `bot_message` or `bot_messages.known_bot_users` only.
    """
    entry = config.user_directory
    reconcilers = [source for source in sources if isinstance(source, SlackReconcileSource)]
    if not entry.enabled or not reconcilers:
        return None
    reconcile = reconcilers[0]
    return UserDirectory(lambda: reconcile.client, store, entry.ttl_seconds,
                         config.bot_messages.known_bot_users, clock=clock,
                         failure_cooldown_seconds=entry.failure_cooldown_seconds)


class CoverageMonitor:
    """Process socket transitions on the service thread and wake reconcile workers."""
    def __init__(self, store, pipeline, sources, clock, threshold):
        self.store, self.pipeline, self.sources = store, pipeline, sources
        self.clock, self.threshold = clock, threshold
        self.transitions = queue.SimpleQueue()
        self.open_gaps = {}
        self.started_at = self.stamp(clock())
        self.last_catchup_at = None
        self.gaps = 0
        self.reconcilers = [source for source in sources if isinstance(source, SlackReconcileSource)]
        for source in sources:
            if isinstance(source, SlackSocketSource):
                source.clock = clock
                source.on_connection = lambda kind, at, source=source: self.transitions.put((source, kind, at))
        for source in self.reconcilers:
            source.on_catchup = self.caught_up
            source.on_context = pipeline.record_context
        if self.reconcilers:
            pipeline.reconcile_source = self.reconcilers[0]
        self.new_gap("service", self.started_at, self.started_at, "start")

    @staticmethod
    def stamp(value):
        return datetime.fromtimestamp(value, timezone.utc).astimezone().isoformat()

    def new_gap(self, source, detected, resumed, note):
        gap = self.store.record_gap(source, detected, self.pipeline.last_event_at, resumed, note)
        self.gaps += 1
        LOG.info("Ingest gap id=%d source=%s", gap, source)
        return gap

    def caught_up(self, at):
        self.store.complete_gaps(at)
        self.last_catchup_at = at

    def poll(self):
        while not self.transitions.empty():
            source, kind, at = self.transitions.get_nowait()
            if kind == "disconnected":
                self.open_gaps[source] = self.new_gap("slack_socket", self.stamp(at), None, "disconnect")
            elif kind == "reconnected":
                gap = self.open_gaps.pop(source, None)
                if gap is not None:
                    with self.store._transaction() as db:
                        db.execute("UPDATE ingest_gaps SET resumed_at=? WHERE id=?", (self.stamp(at), gap))
                for reconcile in self.reconcilers:
                    reconcile.request_catchup()
        return any(isinstance(source, SlackSocketSource) and source.disconnected_at is not None
                   and self.clock() - source.disconnected_at > self.threshold for source in self.sources)


def consume_wake_requests(pipeline, store):
    """Offer one lane wake per pending (lane, message id) wake request (P12).

    Requests come from the inbox CLI (`assign`, `reroute`), the console actions and card
    decisions. Each distinct (lane, message id) gets one `wake_engine.offer`, so the
    socket debounce and storm rules apply; only messages still assigned to that lane are
    offered. Every request row read is marked consumed, duplicates included.

    P21 fix1: a request of kind `wake_now` (console Wake now, CLI `wake`) also offers an
    item in state `acked`. Such ids go into `pipeline.wake_now_ids` while their wake is
    pending (debounce or storm batch), so the flush lets them through, and leave it once
    the wake was sent or dropped. Requests of kind `assign` keep the assigned-only rule.
    """
    rows = store.pending_wake_requests()
    groups, wake_now = {}, set()
    for row in rows:
        key = (row["lane"], row["message_id"])
        groups.setdefault(key, []).append(row["id"])
        if row.get("kind") == WAKE_KIND_NOW:
            wake_now.add(key)
    marked = getattr(pipeline, "wake_now_ids", None)
    offered = 0
    with pipeline.lock:
        for (lane, message_id), ids in groups.items():
            message = store.get_message(message_id) if lane and message_id else None
            states = ("assigned", "acked") if (lane, message_id) in wake_now else ("assigned",)
            if message and message.get("lane") == lane and message["state"] in states:
                now_wake = (lane, message_id) in wake_now and marked is not None
                if now_wake:
                    marked.add(message_id)
                wake = pipeline.wake_engine.offer(lane, message, message.get("author"))
                if isinstance(wake, Deliver):
                    pipeline._send("lane", lane, [message_id], inbox=False)
                if getattr(wake, "notice", None) is not None:
                    pipeline._human_notice(message_id, kind="storm")
                    pipeline.wake_engine.notices.clear()
                # Keep the mark only while the wake waits in the engine (debounce or batch).
                if now_wake and not (isinstance(wake, (Defer, Batch))
                                     and getattr(wake, "reason", "debounce") != "disabled"):
                    marked.discard(message_id)
                offered += 1
            store.consume_wake_requests(ids)
    return offered


def run(config, *, once=False, dry_run=False, clock=None, sleep=None, config_name="unknown"):
    clock, sleep = clock or time.time, sleep or time.sleep
    # One wake switch for the engine, the adapter builder and the delivery gate
    # (wake_switch_path): wake_policy.state_dir when set, else service.state_dir.
    switch_differs = wake_dirs_differ(config)
    switch_field = ("wake_policy.state_dir" if config.wake_policy.state_dir != DEFAULT_STATE_DIR
                    else "service.state_dir")
    config = replace(config, wake_policy=replace(config.wake_policy,
                                                state_dir=str(wake_switch_path(config).parent)))
    state = Path(config.service.state_dir)
    with SingleInstanceLock(state / "service.lock"):
        logger = configure_logging(config.service.log_dir, config.service.log_level)
        handlers = [handler for handler in logger.handlers
                    if getattr(handler, "_dispatch_runtime", False)]
        if switch_differs:
            LOG.warning("Wake switch: wake_policy.state_dir and service.state_dir differ; "
                        "wake.off is read from %s", switch_field)
        store = LedgerStore(config.ledger.path, store_body=config.ledger.store_body)
        flags = ControlFlags(state)
        heartbeat = HeartbeatWriter(state / "heartbeat.json",
                                    config.service.heartbeat_seconds, clock)
        sources, started = [], []
        pipeline = sweep = monitor = decisions = membership = None
        shutdown_ok = True
        exit_code = 0
        start_failures = 0

        def attempt(operation, callback):
            try:
                callback()
                return True
            except Exception:
                pipeline.failure(operation)
                return False

        def publish(status="running", force=False):
            counters = pipeline.snapshot()
            counters["failures"] += sweep.failures + sum(
                getattr(source, "failures", 0) for source in sources)
            if decisions is not None:
                counters.update(decisions.snapshot())
                counters["failures"] += decisions.failures
            degraded = monitor.poll() if monitor else False
            heartbeat.write(ok=counters["failures"] == 0 and not degraded,
                            status="degraded" if status == "running" and degraded else status,
                            version=__version__, counters=counters, force=force,
                            started_at=monitor.started_at if monitor else None,
                            last_event_at=pipeline.last_event_at,
                            last_catchup_at=monitor.last_catchup_at if monitor else None,
                            gaps=monitor.gaps if monitor else 0)

        try:
            Path(config.ledger.path).parent.mkdir(parents=True, exist_ok=True)
            store.open()
            store.migrate()
            for lane in config.lanes:
                # Preserve session claims already made through the inbox CLI.
                current = store.get_lane(lane.id)
                store.upsert_lane(lane.id, session_pointer=lane.session if current is None else None,
                                  paused=lane.paused)
            suggester = build_suggester(config)
            try:
                notifiers = build_notifiers(config, store)
            except ValueError:
                raise ConfigError("Cannot build configured notification adapters") from None
            if dry_run:
                notifiers = [_DryRunNotifier() for _ in notifiers]
            wake = WakePolicyEngine(config.wake_policy, clock)
            pipeline = Pipeline(config, store, suggester, notifiers, wake, clock)
            sweep = TimerSweep(config, store, notifiers, clock, human=pipeline.human)
            sweep.lock = pipeline.lock
            directory = card_dir(config)
            if directory and not once and not dry_run:
                # P12: card bot decisions; off without a dispatch_card dir, in --once and --dry-run.
                decisions = DecisionConsumer(config, store, directory)
            sources = build_service_sources(config, pipeline.handle_event, store, once=once)
            monitor = CoverageMonitor(store, pipeline, sources, clock, config.service.gap_alert_seconds)
            membership = build_membership(config, store, sources, clock)
            pipeline.membership = membership
            pipeline.directory = build_user_directory(config, store, sources, clock)
            LOG.info("Service start version=%s config=%s pid=%d", __version__, Path(config_name).name, os.getpid())
            attempt("heartbeat", lambda: publish(force=True))
            for source in sources:
                started.append(source)
                if not attempt("source_start", source.start):
                    exit_code = 1
                    start_failures += 1
            attempt("heartbeat", lambda: publish(force=True))
            if not once and sources and start_failures == len(sources):
                print("Source start failed, see service.log", file=sys.stderr)
                return 1
            if once:
                attempt("timers", lambda: sweep.run_once(clock()))
            else:
                last_flush = last_sweep = clock()
                last_decisions = last_wakes = None
                while True:
                    try:
                        if flags.should_stop():
                            break
                        now = clock()
                        if decisions is not None and (
                                last_decisions is None or now < last_decisions
                                or now - last_decisions >= config.decisions.poll_seconds):
                            def decide():
                                with pipeline.lock:
                                    decisions.run_once()
                            attempt("card_decisions", decide)
                            last_decisions = now
                        if membership is not None:
                            # Every membership.refresh_seconds; skipped while a pass refreshes.
                            attempt("membership", lambda: membership.maybe_refresh(blocking=False))
                        if last_wakes is None or now < last_wakes or now - last_wakes >= WAKE_REQUEST_SECONDS:
                            attempt("wake_requests", lambda: consume_wake_requests(pipeline, store))
                            last_wakes = now
                        if now < last_flush or now - last_flush >= 30:
                            attempt("flush", lambda: pipeline.flush(now))
                            last_flush = now
                        if now < last_sweep or now - last_sweep >= 60:
                            attempt("timers", lambda: sweep.run_once(now))
                            last_sweep = now
                        attempt("heartbeat", publish)
                    except Exception:
                        pipeline.failure("loop")
                    try:
                        sleep(1)
                    except Exception:
                        pipeline.failure("sleep")
        except KeyboardInterrupt:
            pass
        finally:
            for source in reversed(started):
                shutdown_ok = attempt("source_stop", source.stop) and shutdown_ok
            if pipeline is not None:
                shutdown_ok = attempt("shutdown_flush", lambda: pipeline.flush(clock())) and shutdown_ok
                if shutdown_ok:
                    shutdown_ok = attempt("stop_acknowledge", flags.acknowledge_stop)
                attempt("heartbeat", lambda: publish("stopped" if shutdown_ok else "stop_failed", True))
            LOG.info("Service stop ok=%s", shutdown_ok)
            try:
                store.close()
            finally:
                for handler in handlers:
                    logger.removeHandler(handler)
                    handler.close()
        if once and pipeline is not None:
            if start_failures or any(getattr(source, "failures", 0) for source in sources):
                print("Source start failed, see service.log", file=sys.stderr)
            if (pipeline.snapshot()["failures"] or sweep.failures
                    or any(getattr(source, "failures", 0) for source in sources)):
                exit_code = 1
        return exit_code if shutdown_ok else 1


def rehydrate_bodies(config, store, source, days, *, clock=time.time, sleep=time.sleep):
    """Fill null bodies of recent open ledger rows once through the reconcile client.

    Rows qualify when their message ts is within the last `days` days, the body is
    null, the state is one of REHYDRATE_STATES and the row was not deleted. Excluded
    channels are never fetched. Calls are spaced so at most REHYDRATE_CALLS_PER_MINUTE
    requests start in any minute. Returns counts; text is stored, never logged.
    """
    floor = Decimal(f"{max(0.0, clock() - days * 86400):.6f}")
    excluded = set(config.excluded_channels)
    counts = dict(candidates=0, fetched=0, filled=0, missing=0, failures=0, excluded=0)
    rows = store._rows("SELECT id, channel, ts, thread_ts FROM messages WHERE source='slack' "
                       "AND body IS NULL AND state IN (?, ?, ?, ?) "
                       "AND (note IS NULL OR note != 'message_deleted') ORDER BY channel, ts",
                       REHYDRATE_STATES)
    spacing = 60 / REHYDRATE_CALLS_PER_MINUTE
    last_call = None
    for row in rows:
        ts = safe(row["ts"], TIMESTAMP)
        if ts is None or Decimal(ts) < floor:
            continue
        if row["channel"] in excluded:
            counts["excluded"] += 1
            continue
        counts["candidates"] += 1
        if last_call is not None:
            wait = last_call + spacing - clock()
            if wait > 0:
                sleep(wait)
        last_call = clock()
        try:
            message = source.fetch_message(row["channel"], ts, safe(row["thread_ts"], TIMESTAMP))
        except Exception as error:
            counts["failures"] += 1
            LOG.warning("Rehydrate fetch failed id=%s class=%s error=%s", row["id"],
                        getattr(error, "error_class", type(error).__name__),
                        getattr(error, "slack_error", None))
            continue
        counts["fetched"] += 1
        body = message_body(message) if isinstance(message, dict) else None
        if not isinstance(body, str) or not body:
            counts["missing"] += 1
            continue
        try:
            if store.set_body(row["id"], body):
                counts["filled"] += 1
        except Exception as error:
            counts["failures"] += 1
            LOG.warning("Rehydrate store failed id=%s class=%s", row["id"], type(error).__name__)
    LOG.info("Rehydrate bodies days=%d candidates=%d fetched=%d filled=%d missing=%d failures=%d "
             "excluded=%d", days, *(counts[key] for key in
                                    ("candidates", "fetched", "filled", "missing", "failures", "excluded")))
    return counts


def run_rehydrate(config, days, *, client=None, clock=None, sleep=None, stdout=None):
    """One rehydrate pass (exclusive mode). Returns 0, 1 on failures, or 2 when not possible."""
    clock, sleep = clock or time.time, sleep or time.sleep
    stdout = stdout or sys.stdout
    if not config.ledger.store_body:
        print("Body storage is disabled: set ledger.store_body to true", file=sys.stderr)
        return 2
    entries = [entry for entry in config.sources if entry.kind == "slack_reconcile"]
    entry = next((row for row in entries if row.enabled), entries[0] if entries else None)
    if entry is None:
        print("No slack_reconcile source configured", file=sys.stderr)
        return 2
    if not Path(config.ledger.path).exists():
        print("Ledger not found", file=sys.stderr)
        return 2
    store = LedgerStore(config.ledger.path, store_body=True)
    source = SlackReconcileSource(replace(entry, store_body=True), lambda event: None,
                                  entry.env_file, entry.user_token_key, store, sleep=sleep,
                                  clock=clock, excluded_channels=config.excluded_channels)
    store.open()
    try:
        store.migrate()
        if client is None:
            source.open_client()
        else:
            source.client = client
        counts = rehydrate_bodies(config, store, source, days, clock=clock, sleep=sleep)
    finally:
        source.client = None
        store.close()
    print("rehydrate " + " ".join(f"{key}={value}" for key, value in counts.items()), file=stdout)
    return 1 if counts["failures"] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the lane dispatch service")
    parser.add_argument("--config", required=True, metavar="PATH")
    parser.add_argument("--once", action="store_true", help="Run one reconcile pass and exit")
    parser.add_argument("--dry-run", action="store_true", help="Log notifications without calling adapters")
    parser.add_argument("--rehydrate-bodies", type=int, metavar="DAYS",
                        help="Fill missing bodies of open rows from the last DAYS days once and exit")
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return error.code
    if args.rehydrate_bodies is not None and (args.once or args.dry_run or args.rehydrate_bodies <= 0):
        print("Invalid command arguments", file=sys.stderr)
        return 2
    try:
        config = load_config(args.config)
        if args.rehydrate_bodies is not None:
            try:
                return run_rehydrate(config, args.rehydrate_bodies)
            except Exception:
                # Neither exception strings nor tracebacks are safe to print here.
                print("Rehydrate failed", file=sys.stderr)
                return 1
        return run(config, once=args.once or config.service.once, dry_run=args.dry_run, config_name=args.config)
    except ConfigError:
        print("Invalid service configuration", file=sys.stderr)
        return 2
    except AlreadyRunning:
        print("Service is already running", file=sys.stderr)
        return 3
    except Exception:
        # Neither exception strings nor tracebacks are safe to print here.
        print("Service failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
