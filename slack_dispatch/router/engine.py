"""Routing decisions without mutations or classifier calls."""
import re
from dataclasses import dataclass

from slack_dispatch.ingest.user_directory import is_bot_message, message_author

from .rules import ChannelDefault, ExplicitTag, PersonChannel, ScopeClaim, ThreadInherit, first_match


# P26: the reason (ledger note) of a post without attention in a mention-only channel.
NO_MENTION = "excluded:no_mention"


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


def message_is_bot(message, config):
    """The precomputed flag when the message carries `is_bot` (events from the pipeline and
    ledger rows), else the P16 rule without a directory. The router never calls Slack."""
    if message.get("is_bot") is not None:
        return bool(message["is_bot"])
    return is_bot_message(message, config)


def attention_for(message, config):
    """`owner_mention` for an exact mention of a mention user or the owner, else
    `always_human` for a protected author, else None."""
    protected = config.always_human
    mentions = set(re.findall(r"<@([A-Za-z0-9]+)(?:\|[^>]*)?>", message.get("text") or ""))
    targets = set(protected.mention_users)
    if protected.owner_user:
        targets.add(protected.owner_user)
    attention = "always_human" if message_author(message) in protected.users else None
    if mentions & targets:
        attention = "owner_mention"
    return attention


def is_mention_only(message, config):
    """True for a channel whose channel_defaults entry sets `mention_only: true` (P26) or
    `mention_only: owner` (P30)."""
    channel = message.get("channel")
    return (channel in getattr(config, "mention_only_channels", ())
            or channel in getattr(config, "mention_only_owner_channels", ()))


def is_mention_only_owner(message, config):
    """True for a channel whose channel_defaults entry sets `mention_only: owner` (P30)."""
    return message.get("channel") in getattr(config, "mention_only_owner_channels", ())


def gate_attention(message, config, attention):
    """The attention that opens a mention-only channel: any attention in a P26 channel, only
    `owner_mention` in a P30 owner mode channel (a protected author alone does not)."""
    if attention != "owner_mention" and is_mention_only_owner(message, config):
        return None
    return attention


def route(message, config, lookup_binding, lookup_scope=None) -> Decision:
    if lookup_scope is None:
        lookup_scope = scope_lookup(lookup_binding)
    lanes = {lane.id for lane in config.lanes}
    if message.get("channel") in config.excluded_channels:
        return Decision(None, "excluded", "excluded")
    bot = message_is_bot(message, config)
    if bot and config.bot_messages.policy != "route_all":
        # P26: bots never carry attention under these policies, so a mention-only channel
        # never routes them to its default lane or a channel claim.
        mention_only = is_mention_only(message, config)
        if config.bot_messages.policy == "route_or_exclude" and not mention_only:
            # Confirmed channel and dm claims act as ledger-backed channel defaults (P13).
            result = first_match(message, (
                ScopeClaim(lookup_scope, lanes=lanes, kinds=("channel", "dm")),
                ChannelDefault(config.channel_defaults, config.dm_defaults),
            ))
            if result is not None:
                return Decision(result.lane, result.reason, result.rule or result.reason)
        return Decision(None, "excluded:bot", "excluded")
    attention = attention_for(message, config)
    if not gate_attention(message, config, attention) and is_mention_only(message, config):
        # P26: without attention only a reply in a bound thread gets through (the same
        # result ThreadInherit gives first in the normal order); anything else is excluded.
        # P30: in owner mode only `owner_mention` counts here; a bound reply keeps its
        # normal attention (None in a P26 channel, where attention never reaches here).
        inherited = ThreadInherit(lookup_binding).match(message)
        if inherited is None:
            return Decision(None, NO_MENTION, "excluded")
        return Decision(inherited.lane, inherited.reason, inherited.reason, attention)
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
