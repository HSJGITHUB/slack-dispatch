"""Jarvis dispatch card requests for immediate human-queue notices (P12).

A `dispatch_card` adapter (purpose `human_queue`) writes one `dispatch_card/1` request per
immediate notice to `<dir>/cards/<message_id_safe>.json`. Jarvis posts it to the operator
as a card with one button per lane plus `direct` and `exclude`, and writes the click back
as a decision file that `slack_dispatch.decisions` consumes. Cards are only for messages
still in the human queue (state `human_pending`), and the direct lane gets no lane button
because the fixed `direct` button already stands for it.

The request is written to `<dir>/cards/.tmp/<name>.part` and then renamed into
`cards/`, so Jarvis never reads a partial file. Digests, timers and storm notices never
produce cards; the ids-only `notify` is a no-op. Bodies are never logged.
"""
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

LOG = logging.getLogger(__name__)
SCHEMA = "dispatch_card/1"
HUMAN_QUEUE = "human_queue"
MAX_LANES = 5
TITLE_CHARS = 120
BODY_CHARS = 1500
ATTENTION = ("owner_mention", "always_human")
# Lane ids Jarvis accepts in `lanes`; `direct` and `exclude` are its fixed buttons.
JARVIS_LANE = re.compile(r"[a-z0-9][a-z0-9_-]{0,29}")
RESERVED = ("direct", "exclude")


def safe_name(message_id):
    """File name stem shared with Jarvis: `:` and `.` become `_`."""
    return str(message_id).replace(":", "_").replace(".", "_")


def card_wanted(spec, kind, immediate, mode, state=None):
    """Whether a human notice offer queues a card for this adapter.

    Cards follow attention notices offered for immediate delivery. With
    `only_immediate: false` and `human_queue.mode: immediate`, ordinary per-item notices
    produce cards too. Timer, storm and digest notices never do. Only a message still in
    the human queue (`state == "human_pending"`) gets a card: an attention item the router
    already assigned to a lane keeps its FYI notice on the other adapters but no card.
    """
    if state != "human_pending":
        return False
    if kind.startswith("attention:") and immediate:
        return True
    return (not getattr(spec, "only_immediate", True) and mode == "immediate"
            and (kind == "message" or kind.startswith("attention:")))


def permalink(domain, channel, ts):
    """`https://<domain>.slack.com/archives/<channel>/p<ts without the dot>`, or None."""
    if not domain or not channel or not ts:
        return None
    return f"https://{domain}.slack.com/archives/{channel}/p{str(ts).replace('.', '')}"


def card_lanes(config, store, message):
    """At most five lane ids: classifier suggestion, channel default, thread binding, rest A-Z.

    The direct lane (`console.direct_lane`) is left out because the card's fixed `direct`
    button already assigns to it; with no direct lane set nothing extra is left out.
    """
    direct = getattr(getattr(config, "console", None), "direct_lane", None) or None
    allowed = [lane.id for lane in config.lanes
               if lane.id not in (HUMAN_QUEUE, direct) and lane.id not in RESERVED
               and JARVIS_LANE.fullmatch(lane.id)]
    ordered = []

    def add(lane):
        if lane in allowed and lane not in ordered:
            ordered.append(lane)

    add(message.get("classifier_suggestion"))
    channel = message.get("channel")
    for defaults in (config.channel_defaults, config.dm_defaults):
        add(defaults.get(channel))
    lookup = getattr(store, "lookup_binding", None)
    if callable(lookup) and channel and message.get("ts"):
        add(lookup(message.get("source") or "slack", channel,
                   message.get("thread_ts") or message["ts"]))
    for lane in sorted(allowed):
        add(lane)
    return ordered[:MAX_LANES]


def console_link(config, message_id):
    base = getattr(getattr(config, "console", None), "base_url", None)
    if not isinstance(base, str) or not base.strip():
        return None
    return base.rstrip("/") + "/#msg=" + quote(message_id, safe=":._-")


def card_body(stored, summary, link):
    """First 1500 characters of the stored body, else the ids-only notice summary.

    dispatch_card/1 has no console_url key (Jarvis rejects unknown keys), so the console
    link becomes the last body line when it is not already there.
    """
    text = stored if isinstance(stored, str) and stored.strip() else (summary or "")
    if link and link not in text:
        suffix = "\n\nOpen: " + link
        return text[:max(0, BODY_CHARS - len(suffix))] + suffix
    return text[:BODY_CHARS]


def build_card(config, store, message_id, summary, *, workspace_domain=None, now=None):
    """The dispatch_card/1 request for one message, or None when no lane can be offered."""
    message = store.get_message(message_id, include_body=True) or {}
    lanes = card_lanes(config, store, message)
    if not lanes:
        return None
    attention = message.get("attention") if message.get("attention") in ATTENTION else None
    channel = message.get("channel") or "-"
    names = getattr(getattr(config, "console", None), "channels", {}) or {}
    label = attention.replace("_", " ") if attention else "human queue"
    title = f"[slack-dispatch] {label} {names.get(channel, channel)}"[:TITLE_CHARS]
    suggestion = message.get("classifier_suggestion")
    stored = message.get("body", message.get("text"))
    return {"schema": SCHEMA, "message_id": message_id, "title": title,
            "body": card_body(stored, summary, console_link(config, message_id)),
            "lanes": lanes, "suggested_lane": suggestion if suggestion in lanes else None,
            "attention": attention,
            "permalink": permalink(workspace_domain, message.get("channel"), message.get("ts")),
            "created_at": (now or datetime.now().astimezone()).isoformat()}


def write_card(directory, card):
    """Write `cards/.tmp/<name>.part`, flush, then rename to `cards/<message_id_safe>.json`."""
    cards = Path(directory) / "cards"
    staging = cards / ".tmp"
    staging.mkdir(parents=True, exist_ok=True)
    name = safe_name(card["message_id"])
    part = staging / f"{name}.{uuid4().hex}.part"
    with part.open("x", encoding="utf-8") as handle:
        json.dump(card, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    target = cards / (name + ".json")
    os.replace(part, target)
    return target


class DispatchCardNotifier:
    """Card request writer for the human queue; see the module docstring."""

    def __init__(self, directory, config, store, *, only_immediate=True, workspace_domain=None):
        self.directory = Path(directory)
        self.config, self.store = config, store
        self.only_immediate = only_immediate
        self.workspace_domain = workspace_domain

    def notify(self, lane, message_ids):
        """Ids-only notices (timers in immediate mode) never produce cards."""
        return None

    def write(self, message_ids, metadata):
        """Write one card per id of an immediate notice; return the written paths."""
        written = []
        for message_id in message_ids:
            card = build_card(self.config, self.store, message_id, metadata.get("summary"),
                              workspace_domain=self.workspace_domain)
            if card is None:
                LOG.warning("Dispatch card skipped: no lane id is valid for a card id=%s", message_id)
                continue
            written.append(write_card(self.directory, card))
            LOG.info("Dispatch card written id=%s lanes=%d", message_id, len(card["lanes"]))
        return written
