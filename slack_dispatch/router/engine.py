"""Routing decisions without mutations or classifier calls."""
import re
from dataclasses import dataclass

from .rules import ChannelDefault, ExplicitTag, PersonChannel, ThreadInherit, first_match


@dataclass(frozen=True)
class Decision:
    lane: str | None
    reason: str
    rule: str | None
    attention: str | None = None


def route(message, config, lookup_binding) -> Decision:
    if message.get("channel") in config.excluded_channels:
        return Decision(None, "excluded", "excluded")
    author = message.get("user", message.get("author"))
    bot = bool(message.get("is_bot") or message.get("bot_id") or not author
               or author in config.bot_messages.known_bot_users)
    if bot and config.bot_messages.policy != "route_all":
        if config.bot_messages.policy == "route_or_exclude":
            result = ChannelDefault(config.channel_defaults, config.dm_defaults).match(message)
            if result is not None:
                return Decision(result.lane, result.reason, result.reason)
        return Decision(None, "excluded:bot", "excluded")
    protected = config.always_human
    mentions = set(re.findall(r"<@([A-Za-z0-9]+)(?:\|[^>]*)?>", message.get("text") or ""))
    targets = set(protected.mention_users)
    if protected.owner_user:
        targets.add(protected.owner_user)
    attention = "always_human" if author in protected.users else None
    if mentions & targets:
        attention = "owner_mention"
    result = first_match(message, (
        ThreadInherit(lookup_binding),
        ExplicitTag(config.explicit_tags, config.explicit_tag_pattern),
        ChannelDefault(config.channel_defaults, config.dm_defaults),
        PersonChannel(config.person_channel_rules),
    ))
    if result is None:
        if attention:
            return Decision(None, "always_human", "always_human", attention)
        return Decision(None, "none", "none")
    return Decision(result.lane, result.reason, result.reason, attention)
