"""Notifier contract for durable files, local session CLI wakes or assistant DM outboxes.

The lane selects the destination and payloads contain ledger ids only; failures must
surface to the caller while durable pending work remains available for retry.
"""
from typing import Protocol


class Notifier(Protocol):
    def notify(self, lane: str, message_ids: list[str]) -> None: ...
