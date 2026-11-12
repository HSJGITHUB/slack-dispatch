"""Normalize supported Slack events without retaining arbitrary payload fields."""
import re

from .base import Event

IDENTIFIER = r"[A-Z][A-Z0-9]{1,63}"
TIMESTAMP = r"[0-9]{1,20}(?:\.[0-9]{1,10})?"
LABEL = r"[a-z][a-z0-9_]{0,63}"
SUPPORTED = {"message", "reaction_added", "reaction_removed", "file_shared",
             "channel_created", "member_joined_channel"}


def safe(value, pattern):
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def mapping(value):
    return value if isinstance(value, dict) else {}


def normalize_slack_event(envelope_payload: dict, received_at: str, *, store_body=False) -> Event | None:
    payload = mapping(envelope_payload)
    event = mapping(payload.get("event"))
    kind = event.get("type")
    if not isinstance(kind, str) or kind not in SUPPORTED:
        return None
    subtype = safe(event.get("subtype"), LABEL)
    metadata = event
    if subtype == "message_changed":
        metadata = mapping(event.get("message"))
    elif subtype == "message_deleted":
        metadata = mapping(event.get("previous_message"))
    item = mapping(event.get("item"))
    channel = event.get("channel") or event.get("channel_id") or item.get("channel")
    creator = None
    if kind == "channel_created":
        creator = mapping(channel).get("creator")
        channel = mapping(channel).get("id")
    ts = metadata.get("ts") or item.get("ts")
    if subtype == "message_deleted":
        ts = event.get("deleted_ts") or metadata.get("ts")
    ts = safe(ts, TIMESTAMP)
    thread_ts = safe(metadata.get("thread_ts"), TIMESTAMP)
    channel_type = event.get("channel_type")
    result = dict(source="slack", event_id=safe(payload.get("event_id"), r"Ev[A-Za-z0-9]{1,64}"),
                  event_type=kind, subtype=subtype, channel=safe(channel, IDENTIFIER),
                  channel_type=channel_type if channel_type in ("channel", "group", "im", "mpim") else None,
                  user=safe(metadata.get("user") or event.get("user") or event.get("user_id") or creator, IDENTIFIER),
                  ts=ts, thread_ts=thread_ts, is_thread_reply=bool(thread_ts and thread_ts != ts),
                  has_files=bool(metadata.get("files")) or kind == "file_shared",
                  reaction=safe(event.get("reaction"), r"[A-Za-z0-9_+\-]{1,100}") if kind.startswith("reaction_") else None,
                  received_at=received_at)
    for key in ("event_time", "retry_attempt"):
        value = payload.get(key)
        result[key] = value if type(value) is int and value >= 0 else None
    if store_body and kind == "message" and subtype != "message_deleted":
        text = metadata.get("text")
        result["text"] = text if isinstance(text, str) else None
    return result
