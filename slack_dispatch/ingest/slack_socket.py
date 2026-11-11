"""Real-time Socket Mode intake must ACK immediately before processing events.

Deduplicate in the ledger by event_id, falling back to channel+ts within the source;
startup history catch-up belongs to SlackReconcileSource.
"""
from .base import EventHandler


class SlackSocketSource:
    def __init__(self, on_event: EventHandler):
        self.on_event = on_event

    def start(self) -> None:
        raise NotImplementedError("Source startup is planned for the ingest packet")

    def stop(self) -> None:
        raise NotImplementedError("Source shutdown is planned for the ingest packet")
