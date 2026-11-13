"""Construct metadata-only notification adapters without sending anything."""
from pathlib import Path
from copy import copy
from functools import partial
import shlex

from .base import Notifier
from .dispatch_card import DispatchCardNotifier
from .file_drop import FileDropNotifier
from .file_inbox import FileInboxNotifier
from .session_message import NotifyError, SessionMessageNotifier
from .session_resolve import DEFAULT_SESSIONS_DIR, SessionResolver

__all__ = ["Notifier", "NotifyError", "build_notifiers", "effective_lane", "human_queue_target",
           "wake_switch_path"]

DEFAULT_STATE_DIR = "state"
HUMAN_QUEUE_TARGET = "operator"


def wake_switch_path(config):
    """Return the one wake.off path shared by the builder, the delivery gate and the service.

    wake_policy.state_dir wins when it is set to a value other than its default
    (`state`); otherwise service.state_dir is used. The schema fills the default,
    so a default value is treated as unset and a service-only state_dir keeps its switch.
    """
    wake_dir = getattr(config.wake_policy, "state_dir", None) or DEFAULT_STATE_DIR
    service_dir = getattr(getattr(config, "service", None), "state_dir", None)
    if wake_dir != DEFAULT_STATE_DIR or not service_dir:
        return Path(wake_dir) / "wake.off"
    return Path(service_dir) / "wake.off"


def wake_dirs_differ(config):
    """True when wake_policy.state_dir and service.state_dir name different directories."""
    service_dir = getattr(getattr(config, "service", None), "state_dir", None)
    return service_dir is not None and Path(config.wake_policy.state_dir) != Path(service_dir)


def human_queue_target(adapter):
    """Target for a human_queue adapter: template.target when set, else `operator`.

    No lane lookup, session resolution or pause check applies to the operator target.
    """
    template = getattr(adapter, "template", None)
    target = template.get("target") if isinstance(template, dict) else None
    return target if isinstance(target, str) and target else HUMAN_QUEUE_TARGET


def effective_lane(config, store, lane):
    """Return (session, paused) for a lane.

    A non-null configured session wins over the ledger pointer; the ledger pointer
    (set by claim) applies only when the configured session is null. A configured
    paused: true always pauses; otherwise the ledger value applies when set.
    """
    configured = next((row for row in config.lanes if row.id == lane), None)
    current = store.get_lane(lane) or {}
    session = configured.session if configured and configured.session is not None else None
    if session is None:
        session = current.get("session_pointer")
    paused = bool(configured.paused) if configured else False
    if not paused and current.get("paused") is not None:
        paused = bool(current["paused"])
    return session, paused


def build_notifiers(config, store) -> list[Notifier]:
    lanes = {lane.id: lane for lane in config.lanes}
    resolver = SessionResolver(getattr(config, "sessions_dir", None) or DEFAULT_SESSIONS_DIR)

    def resolve(lane, purpose, adapter=None, message_ids=None):
        if purpose == "human_queue":
            # The operator target never depends on the lane table (no pseudo-lane needed).
            return human_queue_target(adapter)
        if purpose == "lane" and (not config.wake_policy.enabled or wake_switch_path(config).exists()):
            return None
        session, paused = effective_lane(config, store, lane)
        if paused or not session:
            return None
        return resolver.resolve_target(lane, session, message_ids=message_ids)

    result = []
    for adapter in config.notify_adapters:
        if adapter.kind == "file_inbox":
            result.append(FileInboxNotifier(adapter.path or config.inbox.root, store.get_message))
        elif adapter.kind == "session_message":
            if not adapter.command_template:
                raise ValueError("Session command template is required")
            result.append(SessionMessageNotifier(adapter.command_template,
                                                 partial(resolve, purpose=adapter.purpose, adapter=adapter),
                                                 dry_run=adapter.dry_run, timeout=adapter.timeout,
                                                 template=adapter.template, lanes=list(lanes),
                                                 lookup=store.get_message))
            # The builder resolver accepts message ids, so skips are counted per (lane, id).
            result[-1].resolver_takes_ids = True
            # Shared resolver: the delivery gate uses it and the heartbeat sums skipped_no_session.
            result[-1].session_resolver = resolver
        elif adapter.kind in ("file_drop", "assistant_dm"):
            result.append(FileDropNotifier(adapter.path, adapter.template))
        elif adapter.kind == "dispatch_card":
            # P12: Jarvis card requests for immediate human-queue notices.
            result.append(DispatchCardNotifier(adapter.path, config, store,
                                               only_immediate=adapter.only_immediate,
                                               workspace_domain=adapter.workspace_domain))
        else:
            raise ValueError("Unsupported notification adapter")
    return result


def notify_human(notifier, ids, metadata):
    """Render generated metadata without extending the ids-only base protocol."""
    if isinstance(notifier, DispatchCardNotifier):
        # Only immediate notices reach a card adapter (the pipeline filters digests).
        notifier.write(ids, metadata)
        return
    if isinstance(notifier, FileInboxNotifier):
        # Human summaries are outbox notices, not lane inbox records.
        FileDropNotifier(Path(notifier.root) / "human_queue-notices",
                         dict(metadata, lane="human_queue", body=metadata["summary"])).notify("human_queue", ids)
        return
    if isinstance(notifier, FileDropNotifier):
        notifier = copy(notifier)
        notifier.template = dict(notifier.template, human_queue=metadata,
                                 body=metadata["summary"], count=metadata["count"],
                                 channels=metadata["channels"])
    elif isinstance(notifier, SessionMessageNotifier):
        notifier = copy(notifier)
        parts = notifier.command_template
        if isinstance(parts, str):
            parts = shlex.split(parts)
        notifier.command_template = [part.replace("{message}", metadata["summary"])
                                     for part in parts]
    notifier.notify("human_queue", ids)
