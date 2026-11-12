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
