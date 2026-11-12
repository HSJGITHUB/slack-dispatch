"""Routing decisions without mutations or classifier calls."""
import re
from dataclasses import dataclass

from .rules import ChannelDefault, ExplicitTag, PersonChannel, ThreadInherit, first_match


@dataclass(frozen=True)
class Decision:
    lane: str | None
    reason: str
    rule: str | None


def route(message, config, lookup_binding) -> Decision:
    if message.get("channel") in config.excluded_channels:
        return Decision(None, "excluded", "excluded")
    protected = config.always_human
    mentions = set(re.findall(r"<@([A-Za-z0-9]+)(?:\|[^>]*)?>", message.get("text") or ""))
    targets = set(protected.mention_users)
    if protected.owner_user:
        targets.add(protected.owner_user)
    if message.get("user", message.get("author")) in protected.users or mentions & targets:
        return Decision(None, "always_human", "always_human")
    result = first_match(message, (
        ThreadInherit(lookup_binding),
        ExplicitTag(config.explicit_tags, config.explicit_tag_pattern),
        ChannelDefault(config.channel_defaults, config.dm_defaults),
        PersonChannel(config.person_channel_rules),
    ))
    if result is None:
        return Decision(None, "none", "none")
    return Decision(result.lane, result.reason, result.reason)
