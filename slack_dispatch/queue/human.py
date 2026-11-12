"""Human queue operations and metadata-only adapter export."""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from slack_dispatch.router.rules import is_reply


@dataclass(frozen=True)
class QueueItem:
    id: str
    channel: str
    ts: str
    thread_ts: str | None
    suggested_lane: str | None
    reason: str | None
    age_seconds: float


def list_pending(store, limit=100) -> list[QueueItem]:
    if limit < 1:
        return []
    now = datetime.now(timezone.utc)
    return [QueueItem(
        row["id"], row["channel"], row["ts"], row.get("thread_ts"),
        row.get("classifier_suggestion"),
        row.get("suggestion_reason") or row.get("route_reason") or row.get("note"),
        max(0.0, (now - datetime.fromisoformat(row["received_at"])).total_seconds()),
    ) for row in store.list_messages(state="human_pending", limit=limit)]


def assign(store, message_id, lane, *, by: str, note: str | None) -> None:
    message = store.get_message(message_id)
    if message is None:
        raise KeyError(message_id)
    if not lane or not by:
        raise ValueError("Lane and reviewer are required")
    store.set_route(message_id, lane, f"human:{by}")
    if note is not None:
        store.set_state(message_id, "assigned", note=note)
    if is_reply(message):
        store.bind_thread(message["source"], message["channel"], message["thread_ts"], lane)


def _all_messages(store, **filters):
    # The contract has no offset cursor. Grow a prefix until it contains all rows.
    limit = 100
    while True:
        rows = store.list_messages(limit=limit, **filters)
        if len(rows) < limit:
            return rows
        limit *= 2


def export_jsonl(store, path):
    rows = _all_messages(store, state="human_pending")
    with Path(path).open("w", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps({"id": row["id"],
                                     "suggested_lane": row.get("classifier_suggestion")}) + "\n")


def promotion_stats(store, channel) -> dict:
    rows = [row for row in _all_messages(store)
            if row.get("channel") == channel
            and (row.get("route_reason") or "").startswith("human:")]
    agreed = sum(row.get("classifier_suggestion") is not None
                 and row.get("classifier_suggestion") == row.get("lane") for row in rows)
    judged = len(rows)
    agreement = agreed / judged if judged else 0.0
    return {"judged": judged, "agreed": agreed, "agreement": agreement,
            "eligible": judged >= 30 and agreement >= 0.9}
