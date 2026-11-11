"""Queue ledger ids only in an assistant outbox for DM delivery through its deduplication gate.

On enqueue or delivery failure, surface the failure and retain pending work for retry
without marking it delivered or sending a separate direct DM.
"""


class AssistantDMNotifier:
    def notify(self, lane: str, message_ids: list[str]) -> None:
        raise NotImplementedError("Notification delivery is not implemented")
