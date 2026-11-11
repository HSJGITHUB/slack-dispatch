"""Planned loop: validate config, lock one instance, open/migrate the ledger, start sources,
then ingest, deduplicate, route, queue notifications and reconcile cursors through shutdown.

Maintain unassigned and unacknowledged timers, debounce and per-lane storm batching,
a heartbeat, the global wake switch and restart recovery of durable pending delivery.
"""


def main() -> None:
    raise NotImplementedError("Resident service is planned for later packets")
