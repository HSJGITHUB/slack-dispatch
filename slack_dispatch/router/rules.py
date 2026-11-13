"""Side-effect-free deterministic routing rules."""
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

Message = dict[str, Any]
DEFAULT_TAG_PATTERN = r"\[(?P<lane>[a-z0-9-]+)\]"


@dataclass(frozen=True)
class Route:
    lane: str
    reason: str
    # Stable rule name when it differs from the reason (P13 ScopeClaim: reason scope:auto).
    rule: str | None = None


class Rule(Protocol):
    def match(self, message: Message) -> Route | None: ...


def is_reply(message):
    return bool(message.get("thread_ts") and message["thread_ts"] != message.get("ts"))


class ThreadInherit:
    def __init__(self, lookup_binding):
        self.lookup_binding = lookup_binding

    def match(self, message):
        if not is_reply(message):
            return None
        lane = self.lookup_binding(message["source"], message["channel"], message["thread_ts"])
        return Route(lane, "thread_inherit") if lane is not None else None


def is_dm(message):
    """A direct conversation: channel type im or mpim, or a D conversation id."""
    return (message.get("channel_type") in ("im", "mpim")
            or str(message.get("channel") or "").startswith("D"))


class ScopeClaim:
    """Confirmed lane scope claims from the ledger (P13).

    `lookup_scope(kind, key)` returns the active confirmed claim (a mapping with `lane`
    and `confirmed_by`) or None. The conversation id is looked up as a dm and as a channel
    claim (dm first in a direct conversation); a person claim matches the author in a
    direct conversation only. Claims for lanes outside `lanes` (when given) are ignored.
    The reason is `scope:auto` for a claim applied automatically, else `scope_claim`.
    """

    def __init__(self, lookup_scope, *, lanes=None, kinds=("channel", "dm", "person")):
        self.lookup_scope = lookup_scope
        self.lanes = None if lanes is None else frozenset(lanes)
        self.kinds = tuple(kinds)

    def _route(self, kind, key):
        if kind not in self.kinds or not key:
            return None
        claim = self.lookup_scope(kind, key)
        if not claim or (self.lanes is not None and claim.get("lane") not in self.lanes):
            return None
        reason = "scope:auto" if claim.get("confirmed_by") == "scope:auto" else "scope_claim"
        return Route(claim["lane"], reason, "scope_claim")

    def match(self, message):
        if self.lookup_scope is None:
            return None
        direct = is_dm(message)
        channel = message.get("channel")
        for kind in (("dm", "channel") if direct else ("channel", "dm")):
            result = self._route(kind, channel)
            if result is not None:
                return result
        if direct:
            return self._route("person", message.get("user", message.get("author")))
        return None


class ExplicitTag:
    def __init__(self, tag_map, pattern):
        self.tag_map = dict(tag_map)
        self.pattern = re.compile(pattern)

    def match(self, message):
        for match in self.pattern.finditer(message.get("text") or ""):
            lane = self.tag_map.get(match.group(0), self.tag_map.get(match.group("lane")))
            if lane is not None:
                return Route(lane, "explicit_tag")
        return None


class ChannelDefault:
    def __init__(self, channel_defaults, dm_defaults):
        self.channel_defaults = dict(channel_defaults)
        self.dm_defaults = dict(dm_defaults)

    def match(self, message):
        channel = message.get("channel")
        is_dm = message.get("channel_type") in ("im", "mpim")
        defaults = self.dm_defaults if is_dm or channel in self.dm_defaults else self.channel_defaults
        lane = defaults.get(channel)
        return Route(lane, "channel_default") if lane is not None else None


class PersonChannel:
    def __init__(self, rules):
        self.rules = tuple(rules)

    def match(self, message):
        for rule in self.rules:
            if (rule.person == message.get("user", message.get("author"))
                    and rule.channel == message.get("channel")):
                return Route(rule.lane, "person_channel")
        return None


def first_match(message: Message, rules: Iterable[Rule]) -> Route | None:
    for rule in rules:
        result = rule.match(message)
        if result is not None:
            return result
    return None
