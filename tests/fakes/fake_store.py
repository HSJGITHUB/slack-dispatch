"""In-memory contract fake. No credentials or external connections."""
from dataclasses import dataclass
from copy import deepcopy
from os import PathLike


@dataclass
class RecordResult:
    message_id: str | None
    created: bool
    duplicate: bool
    kind: str


class FakeStore:
    def __init__(self, path=None, *, messages=None):
        # Seeded P2 fakes expose bodies by default; P3 path fakes redact them.
        if messages is None and path is not None and not isinstance(path, (str, bytes, PathLike)):
            messages = path
        self._include_body_default = messages is not None
        self.messages = {row["id"]: deepcopy(row) for row in (() if messages is None else messages)}
        self.calls = []
        self.events = {}
        self.lanes = {}
        self.bindings = {}
        self.cursors = {}
        self.reactions = {}

    def open(self):
        pass

    def migrate(self):
        pass

    def close(self):
        pass

    def record_event(self, event):
        event_id = event["event_id"]
        if event_id in self.events:
            previous = self.events[event_id]
            return RecordResult(previous.message_id, False, True, previous.kind)
        kind = event["event_type"]
        mid = f"{event['source']}:{event['channel']}:{event['ts']}" if event.get("channel") and event.get("ts") else None
        created = False
        if kind == "message" and mid:
            created = mid not in self.messages
            row = self.messages.setdefault(mid, {"id": mid, "state": "new", "lane": None})
            row.update(event)
            if event.get("subtype") == "message_changed":
                row.update(edited_at=event.get("received_at"), followup=True)
        elif kind in ("reaction_added", "reaction_removed") and mid:
            rows = self.reactions.setdefault(mid, [])
            existing = next((r for r in rows if r["user"] == event["user"] and r["reaction"] == event["reaction"]), None)
            if existing is None:
                existing = {"message_id": mid, "user": event["user"], "reaction": event["reaction"], "added_at": None, "removed_at": None}
                rows.append(existing)
            existing["added_at" if kind == "reaction_added" else "removed_at"] = event.get("received_at")
        elif kind == "file_shared" and mid in self.messages:
            self.messages[mid]["has_files"] = True
        elif kind in ("channel_created", "member_joined_channel"):
            kind, mid = "channel_meta", None
        result = RecordResult(mid, created, False, kind)
        self.events[event_id] = result
        return result

    def get_message(self, message_id, *, include_body=None):
        if include_body is None:
            include_body = self._include_body_default
        row = self.messages.get(message_id)
        if row is None:
            return None
        return deepcopy({k: v for k, v in row.items() if include_body or k not in ("body", "text")})

    def list_messages(self, *, state=None, lane=None, since=None, limit=100):
        rows = sorted(self.messages.values(), key=lambda row: (row.get("received_at", ""), row["id"]))
        return [self.get_message(row["id"]) for row in rows
                if (state is None or row.get("state") == state)
                and (lane is None or row.get("lane") == lane)
                and (since is None or row.get("received_at", "") >= since)][:limit]

    def set_route(self, message_id, lane, reason, *, state="assigned"):
        self.calls.append(("set_route", message_id, lane))
        self.messages[message_id].update(lane=lane, route_reason=reason, state=state)

    def set_state(self, message_id, state, *, note=None):
        self.calls.append(("set_state", message_id, state))
        self.messages[message_id]["state"] = state
        if note is not None:
            self.messages[message_id]["note"] = note

    def set_suggestion(self, message_id, lane, reason, classifier_version):
        self.calls.append(("set_suggestion", message_id, lane))
        self.messages[message_id].update(classifier_suggestion=lane, suggestion_reason=reason,
                                         classifier_version=classifier_version)

    def mark_followup(self, message_id):
        self.messages[message_id]["followup"] = True

    def bind_thread(self, source, channel, thread_ts, lane):
        self.calls.append(("bind_thread", source, channel, thread_ts, lane))
        self.bindings[(source, channel, thread_ts)] = lane

    def lookup_binding(self, source, channel, thread_ts):
        return self.bindings.get((source, channel, thread_ts))

    def upsert_lane(self, lane_id, *, session_pointer=None, paused=None):
        row = self.lanes.setdefault(lane_id, {"id": lane_id})
        if session_pointer is not None:
            row["session_pointer"] = session_pointer
        if paused is not None:
            row["paused"] = paused

    def get_lane(self, lane_id):
        row = self.lanes.get(lane_id)
        return dict(row) if row is not None else None

    def list_lanes(self):
        return [dict(row) for row in self.lanes.values()]

    def get_cursor(self, source, channel):
        return self.cursors.get((source, channel))

    def set_cursor(self, source, channel, cursor):
        self.cursors[(source, channel)] = cursor

    def list_reactions(self, message_id):
        return [dict(row) for row in self.reactions.get(message_id, [])]


def message(**overrides):
    row = dict(id="slack:C0000000001:2", source="slack", channel="C0000000001",
               channel_type="channel", ts="2", thread_ts=None, user="U0000000001",
               received_at="2026-01-01T00:00:00+00:00", state="new", text="[support]")
    row.update(overrides)
    return row
