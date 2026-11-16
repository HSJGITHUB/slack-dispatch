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


# P25 operator queue: everything only the operator can handle.
#
# The operator queue has two kinds of rows. `undecided`: messages in `human_pending` (no lane
# could be resolved). `direct`: messages the router or the operator put in the configured
# direct lane (`console.direct_lane`) that are still open (`assigned` or `acked`). The direct
# lane normally has no session, so nobody else will handle them. Context rows and owner posts
# are never items and are left out. The heartbeat `human_pending` counter and the notice
# pipeline keep counting `human_pending` only.

QUEUE_KINDS = ("undecided", "direct")
DIRECT_STATES = ("assigned", "acked")
ALL_ROWS = 2**63 - 1  # SQLite's largest LIMIT


def direct_rows(store, direct_lane, limit=None):
    """Open items of the direct lane (`assigned` or `acked`), oldest first; [] without a lane.

    fix1: `limit` keeps the first rows only (the CLI `queue` listing keeps its 100 row cap)."""
    if not direct_lane:
        return []
    from slack_dispatch.console.actions import inbox_refusal  # lazy: no import cycle
    rows = []
    for state in DIRECT_STATES:
        rows.extend(store.list_messages(state=state, lane=direct_lane, limit=ALL_ROWS))
    rows.sort(key=lambda row: (row.get("received_at") or "", row["id"]))
    rows = [row for row in rows if not inbox_refusal(dict(row))]
    return rows if limit is None else rows[:limit]


def operator_queue(store, direct_lane, limit=None):
    """`{"undecided": [...], "direct": [...]}` ledger rows of the operator queue (P25).

    fix1: `limit` caps each kind (oldest first); None returns every row."""
    return {"undecided": store.list_messages(state="human_pending",
                                             limit=ALL_ROWS if limit is None else limit),
            "direct": direct_rows(store, direct_lane, limit)}


def queue_counts(store, direct_lane):
    """`{"undecided": N, "direct": M}` for digests, the CLI and the console tab label (P25)."""
    rows = operator_queue(store, direct_lane)
    return {kind: len(rows[kind]) for kind in QUEUE_KINDS}


def digest_counts_text(counts, direct_lane=None):
    """One line for a human queue digest: `Operator queue: undecided=N, direct=M (lane X)`.

    fix1: the pipeline appends it as the last line of a human queue digest when a direct lane
    is configured; the CLI `queue --kind` or `--counts` prints the same counts."""
    text = f"Operator queue: undecided={counts.get('undecided', 0)}, direct={counts.get('direct', 0)}"
    return text + (f" (lane {direct_lane})" if direct_lane else "")
