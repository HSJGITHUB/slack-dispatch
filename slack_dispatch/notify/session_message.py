"""Wake a local agent session through a headless CLI call with ledger ids only.

On CLI failure, surface the error and keep durable inbox work pending for retry
without marking the wake delivered.
"""


class SessionMessageNotifier:
    def notify(self, lane: str, message_ids: list[str]) -> None:
        raise NotImplementedError("Notification delivery is not implemented")
