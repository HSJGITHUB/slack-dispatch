"""Deliver ledger ids only by appending to the durable per-lane JSONL inbox.

On append failure, surface the error and leave ledger work pending for retry without
reporting successful delivery.
"""


class FileInboxNotifier:
    def notify(self, lane: str, message_ids: list[str]) -> None:
        raise NotImplementedError("Notification delivery is not implemented")
