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


def message_body(message):
    """Flatten supported rich text without retaining opaque block payloads."""
    def walk(node):
        if not isinstance(node, dict):
            return ""
        kind = node.get("type")
        if kind == "text":
            return node.get("text") if isinstance(node.get("text"), str) else ""
        if kind == "user":
            return "<@" + str(node.get("user_id", "")) + ">"
        if kind == "channel":
            return "<#" + str(node.get("channel_id", "")) + ">"
        if kind == "link":
            return node.get("url") if isinstance(node.get("url"), str) else ""
        if kind == "emoji":
            return ":" + str(node.get("name", "")) + ":"
        if kind in ("line_break", "newline"):
            return "\n"
        if kind in ("rich_text", "rich_text_list"):
            return "\n".join(walk(child) for child in node.get("elements", []))
        if kind in ("rich_text_section", "rich_text_quote", "rich_text_preformatted"):
            return "".join(walk(child) for child in node.get("elements", []))
        return ""

    body = "\n".join(filter(None, (walk(block) for block in message.get("blocks", []) or [])))
    if body:
        return body
    if isinstance(message.get("text"), str) and message["text"]:
        return message["text"]
    files = ["[file] " + str(item.get("name") or item.get("title"))
             for item in message.get("files", []) or []
             if isinstance(item, dict) and (item.get("name") or item.get("title"))]
    return "\n".join(files) if files else message.get("text")


# P24: raw mrkdwn tokens that message_body renders differently from rich text blocks.
_ANGLE_TOKEN = re.compile(r"<([^<>]*)>")
# fix1: only formatting markers at word boundaries drop (`*bold*`, `_italic_`, `~strike~`,
# `` `code` ``, code fences, a line-leading bullet); `5*3` and `snake_case` keep theirs.
_FENCE = re.compile(r"```")
_SPAN = re.compile(r"(?<![\w*_~`])([*_~`])(?=\S)(.+?)(?<=\S)\1(?![\w*_~`])", re.S)
_BULLET = re.compile(r"(?m)^\s*•\s*")
_QUOTE = re.compile(r"(?m)^\s*>\s?")
_ENTITIES = (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"))


def _angle(match):
    inner = match.group(1)
    target = inner.split("|", 1)[0]
    if target.startswith(("@", "#")):
        return "<" + target + ">"
    if target.startswith("!"):
        return ""  # broadcasts and user groups: message_body renders no text for them
    return target


def comparable_text(value):
    """P24: a stored body or an edit text reduced to one comparison form, so a body rendered
    from rich text blocks (message_body) and the raw mrkdwn `text` of the same message
    compare equal. Links and labelled mentions keep their target only (`<url|label>` ->
    `url`, `<@U1|name>` -> `<@U1>`), broadcasts drop, HTML entities decode, formatting
    markers at word boundaries (`*bold*`, `_italic_`, `~strike~`, `` `code` ``, code
    fences, line-leading bullets and quote prefixes) drop and whitespace runs collapse to
    one space. Markers inside a word (`5*3`, `snake_case`) stay. Used for edit comparison
    only; nothing is stored in this form. None stays None."""
    if not isinstance(value, str):
        return None
    text = _ANGLE_TOKEN.sub(_angle, value)
    for entity, char in _ENTITIES:
        text = text.replace(entity, char)
    text = _QUOTE.sub("", text)
    text = _BULLET.sub("", text)
    text = _FENCE.sub("", text)
    for _ in range(3):  # nested spans such as *_bold italic_*
        stripped = _SPAN.sub(r"\2", text)
        if stripped == text:
            break
        text = stripped
    return " ".join(text.split())


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
    result["bot_id"] = safe(metadata.get("bot_id"), IDENTIFIER)
    result["reactions"] = [dict(name=item.get("name"), users=item.get("users", []))
                           for item in metadata.get("reactions", []) or [] if isinstance(item, dict)]
    if isinstance(metadata.get("files"), list):
        result["files"] = []
        for item in metadata["files"]:
            if not isinstance(item, dict):
                continue
            file = {"id": safe(item.get("id"), IDENTIFIER)}
            if store_body:
                file.update({key: item[key] for key in ("name", "title") if isinstance(item.get(key), str)})
            result["files"].append(file)
    if store_body and kind == "message" and subtype != "message_deleted":
        text = message_body(metadata)
        result["text"] = text if isinstance(text, str) else None
    return result
