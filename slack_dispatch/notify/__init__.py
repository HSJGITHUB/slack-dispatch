"""Construct metadata-only notification adapters without sending anything."""
from pathlib import Path
from copy import copy
from functools import partial
import shlex

from .base import Notifier
from .file_drop import FileDropNotifier
from .file_inbox import FileInboxNotifier
from .session_message import NotifyError, SessionMessageNotifier

__all__ = ["Notifier", "NotifyError", "build_notifiers"]


def build_notifiers(config, store) -> list[Notifier]:
    lanes = {lane.id: lane for lane in config.lanes}

    def resolve(lane, purpose):
        if purpose == "lane" and (not config.wake_policy.enabled or (Path(config.wake_policy.state_dir) / "wake.off").exists()):
            return None
        current = store.get_lane(lane)
        configured = lanes.get(lane)
        paused = current.get("paused") if current else None
        if paused is None:
            paused = configured.paused if configured else False
        if paused:
            return None
        return current.get("session_pointer") if current else (configured.session if configured else None)

    result = []
    for adapter in config.notify_adapters:
        if adapter.kind == "file_inbox":
            result.append(FileInboxNotifier(adapter.path or config.inbox.root, store.get_message))
        elif adapter.kind == "session_message":
            if not adapter.command_template:
                raise ValueError("Session command template is required")
            result.append(SessionMessageNotifier(adapter.command_template, partial(resolve, purpose=adapter.purpose),
                                                 dry_run=adapter.dry_run, timeout=adapter.timeout))
        elif adapter.kind in ("file_drop", "assistant_dm"):
            result.append(FileDropNotifier(adapter.path, adapter.template))
        else:
            raise ValueError("Unsupported notification adapter")
    return result


def notify_human(notifier, ids, metadata):
    """Render generated metadata without extending the ids-only base protocol."""
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
