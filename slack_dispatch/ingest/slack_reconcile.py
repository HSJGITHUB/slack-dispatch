"""Compare channel history against a durable per-channel cursor at startup and periodically.

Unlike real-time Socket Mode ACK handling, this catches up missed history and shares
ledger deduplication by event_id or channel+ts within the source.
"""
from .base import EventHandler


class SlackReconcileSource:
    def __init__(self, on_event: EventHandler):
        self.on_event = on_event

    def start(self) -> None:
        raise NotImplementedError("Source startup is planned for the ingest packet")

    def stop(self) -> None:
        raise NotImplementedError("Source shutdown is planned for the ingest packet")
