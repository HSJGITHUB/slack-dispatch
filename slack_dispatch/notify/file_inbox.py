"""Durable inbox adapter."""
from slack_dispatch.inbox.files import append, record


class FileInboxNotifier:
    def __init__(self, root, lookup=None):
        self.root = root
        self.lookup = lookup

    def notify(self, lane, message_ids):
        records = []
        for message_id in message_ids:
            message = self.lookup(message_id) if self.lookup else None
            if message is None:
                source, channel, ts = message_id.split(":", 2)
                message = {"channel": channel, "ts": ts}
            records.append(record(dict(message, id=message_id)))
        append(self.root, lane, records)
