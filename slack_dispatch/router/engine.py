"""Routing decisions without mutations or classifier calls."""
import re
from dataclasses import dataclass

from .rules import ChannelDefault, ExplicitTag, PersonChannel, ScopeClaim, ThreadInherit, first_match


@dataclass(frozen=True)
class Decision:
    lane: str | None
    reason: str
    rule: str | None
    attention: str | None = None


def scope_lookup(lookup_binding):
    """The ledger's confirmed scope claim lookup (P13) behind a bound `lookup_binding`.

    Callers pass `store.lookup_binding`; its store also answers `lookup_scope_claim`.
    Plain callables and stores without the method yield None (no ScopeClaim routing).
    """
    owner = getattr(lookup_binding, "__self__", None)
    lookup = getattr(owner, "lookup_scope_claim", None)
    return lookup if callable(lookup) else None


def route(message, config, lookup_binding, lookup_scope=None) -> Decision:
    if lookup_scope is None:
        lookup_scope = scope_lookup(lookup_binding)
    lanes = {lane.id for lane in config.lanes}
    if message.get("channel") in config.excluded_channels:
        return Decision(None, "excluded", "excluded")
    author = message.get("user", message.get("author"))
    bot = bool(message.get("is_bot") or message.get("bot_id") or not author
               or author in config.bot_messages.known_bot_users)
    if bot and config.bot_messages.policy != "route_all":
        if config.bot_messages.policy == "route_or_exclude":
            # Confirmed channel and dm claims act as ledger-backed channel defaults (P13).
            result = first_match(message, (
                ScopeClaim(lookup_scope, lanes=lanes, kinds=("channel", "dm")),
                ChannelDefault(config.channel_defaults, config.dm_defaults),
            ))
            if result is not None:
                return Decision(result.lane, result.reason, result.rule or result.reason)
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
        ScopeClaim(lookup_scope, lanes=lanes),
        ExplicitTag(config.explicit_tags, config.explicit_tag_pattern),
        ChannelDefault(config.channel_defaults, config.dm_defaults),
        PersonChannel(config.person_channel_rules),
    ))
    if result is None:
        if attention:
            return Decision(None, "always_human", "always_human", attention)
        return Decision(None, "none", "none")
    return Decision(result.lane, result.reason, result.rule or result.reason, attention)
