"""Resident orchestration for the existing intake, ledger and notification packages."""
import argparse
from dataclasses import replace
from datetime import datetime
import logging
from pathlib import Path
import sys
import time

from slack_dispatch import __version__
from slack_dispatch.config.loader import ConfigError, load_config
from slack_dispatch.ingest import build_sources
from slack_dispatch.ingest.slack_reconcile import SlackReconcileSource
from slack_dispatch.ingest.slack_socket import SlackSocketSource
from slack_dispatch.ingest.normalize import normalize_slack_event
from slack_dispatch.ledger.store import LedgerStore
from slack_dispatch.notify import build_notifiers
from slack_dispatch.notify.wake import WakePolicyEngine
from slack_dispatch.pipeline import Pipeline
from slack_dispatch.router.classifier import AnthropicSuggester, NullSuggester
from slack_dispatch.runtime import (
    AlreadyRunning, ControlFlags, HeartbeatWriter, SingleInstanceLock, configure_logging,
)
from slack_dispatch.timers import TimerSweep

LOG = logging.getLogger(__name__)


class _DryRunNotifier:
    service_dry_run = True

    def notify(self, lane, message_ids):
        LOG.info("Dry-run notification: lane=%s count=%d ids=%s", lane,
                 len(message_ids), list(message_ids))


def _is_bot(metadata, known=()):
    return bool(metadata.get("is_bot") or metadata.get("bot_id") or not metadata.get("user")
                or metadata.get("user") in known)


class _ServiceSocketSource(SlackSocketSource):
    """Preserve bot identity before the legacy normalizer drops bot_id."""
    def _receive(self, client, request):
        try:
            client.send_socket_mode_response(self._response_class(envelope_id=request.envelope_id))
            if request.type == "disconnect":
                self._observe(False)
                return
            if request.type != "events_api" or self._stopped.is_set():
                return
            payload = dict(request.payload)
            retry = getattr(request, "retry_attempt", None)
            if retry is not None:
                payload["retry_attempt"] = retry
            event = normalize_slack_event(payload, datetime.now().astimezone().isoformat(),
                                          store_body=self.config.store_body)
            if event is not None:
                raw = payload.get("event", {})
                if raw.get("subtype") in ("message_changed", "message_deleted"):
                    raw = raw.get("message", raw.get("previous_message", {}))
                event["is_bot"] = _is_bot(raw, self.known_bot_users)
                self.on_event(event)
        except Exception:
            self.failures += 1


class _ServiceReconcileSource(SlackReconcileSource):
    """Keep a channel-local bot map while the existing history pass normalizes."""
    def __init__(self, *args, known_bot_users=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.known_bot_users = known_bot_users
        self._bots = {}
        callback = self.on_event
        def deliver(event):
            event = dict(event, is_bot=self._bots.get(event.get("ts"),
                         _is_bot(event, self.known_bot_users)))
            callback(event)
        self.on_event = deliver

    def _call(self, method, **params):
        response = super()._call(method, **params)
        if method == "conversations.history":
            for message in response.get("messages", []):
                if isinstance(message, dict):
                    self._bots[message.get("ts")] = _is_bot(message, self.known_bot_users)
        return response

    def _catch_up_channel(self, channel):
        self._bots.clear()
        try:
            return super()._catch_up_channel(channel)
        finally:
            self._bots.clear()


class _OnceReconcileSource(_ServiceReconcileSource):
    """Reuse startup initialization but stop before the interval thread can poll."""
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
            source.known_bot_users = config.bot_messages.known_bot_users
        result.append(source)
    return result


def run(config, *, once=False, dry_run=False, clock=None, sleep=None):
    clock, sleep = clock or time.time, sleep or time.sleep
    # The service owns the global switch; standalone package defaults stay intact.
    config = replace(config, wake_policy=replace(config.wake_policy,
                                                state_dir=config.service.state_dir))
    state = Path(config.service.state_dir)
    with SingleInstanceLock(state / "service.lock"):
        logger = configure_logging(config.service.log_dir, config.service.log_level)
        handlers = [handler for handler in logger.handlers
                    if getattr(handler, "_dispatch_runtime", False)]
        store = LedgerStore(config.ledger.path, store_body=config.ledger.store_body)
        flags = ControlFlags(state)
        heartbeat = HeartbeatWriter(state / "heartbeat.json",
                                    config.service.heartbeat_seconds, clock)
        sources, started = [], []
        pipeline = sweep = None
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
            heartbeat.write(ok=counters["failures"] == 0, status=status,
                            version=__version__, counters=counters, force=force)

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
            sources = build_service_sources(config, pipeline.handle_event, store, once=once)
            for source in sources:
                started.append(source)
                if not attempt("source_start", source.start):
                    exit_code = 1
                    start_failures += 1
            attempt("heartbeat", publish)
            if not once and sources and start_failures == len(sources):
                print("Source start failed, see service.log", file=sys.stderr)
                return 1
            if once:
                attempt("timers", lambda: sweep.run_once(clock()))
            else:
                last_flush = last_sweep = clock()
                while True:
                    try:
                        if flags.should_stop():
                            break
                        now = clock()
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the lane dispatch service")
    parser.add_argument("--config", required=True, metavar="PATH")
    parser.add_argument("--once", action="store_true", help="Run one reconcile pass and exit")
    parser.add_argument("--dry-run", action="store_true", help="Log notifications without calling adapters")
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return error.code
    try:
        config = load_config(args.config)
        return run(config, once=args.once or config.service.once, dry_run=args.dry_run)
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
