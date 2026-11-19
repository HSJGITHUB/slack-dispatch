"""Routing decisions without mutations or classifier calls."""
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

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


def exact_mention(message, config):
    """True when the text holds an exact Slack mention (`<@U...>` or `<@U...|label>`) of
    `always_human.owner_user` or an `always_human.mention_users` id."""
    protected = config.always_human
    mentions = set(re.findall(r"<@([A-Za-z0-9]+)(?:\|[^>]*)?>", message.get("text") or ""))
    targets = set(protected.mention_users)
    if protected.owner_user:
        targets.add(protected.owner_user)
    return bool(mentions & targets)


# P36: Slack escapes these three characters in message text.
_SLACK_ESCAPES = (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"))


def fold_text(value):
    """P36: the comparison form of a text: Unicode NFKC, case folded, every run of
    whitespace collapsed to one space and the ends trimmed."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def plain_segments(text):
    """P36: the parts of `text` outside Slack entities (`<...>`: links, channel references,
    user mentions with their labels, broadcasts), with the Slack escapes decoded. One
    forward scan; a `<` without a later `>` starts no entity."""
    segments, start = [], 0
    while True:
        opening = text.find("<", start)
        if opening < 0:
            break
        closing = text.find(">", opening + 1)
        if closing < 0:
            break
        segments.append(text[start:opening])
        start = closing + 1
    segments.append(text[start:])
    result = []
    for segment in segments:
        for escaped, plain in _SLACK_ESCAPES:
            segment = segment.replace(escaped, plain)
        result.append(segment)
    return result


# P36: two control characters the fast path inserts: MARK at every edge of a run of ASCII
# letters and digits, SEP between the parts outside entities. In the text they are first
# replaced with a FILLERS character no name holds (all three are neither letters nor
# digits, so boundaries stay the same); a name that holds MARK or SEP, or every filler,
# makes the configuration take the slow path.
_MARK, _SEP = "\x00", "\x01"
_FILLERS = tuple(chr(code) for code in (*range(0x02, 0x09), *range(0x0e, 0x1c)))
_WORD_RUN = re.compile(r"[A-Za-z0-9]+")


def mark_words(value):
    """P36: `value` with `_MARK` on both sides of every maximal run of ASCII letters and
    digits. A bounded name matches a text exactly when the marked name is a substring of
    the marked text: a mark before (after) the name's first (last) ASCII letter or digit
    can only meet a run edge, which is the boundary rule of `contains_name`."""
    return _WORD_RUN.sub(_MARK + r"\g<0>" + _MARK, value)


@dataclass(frozen=True)
class FoldedNames:
    """P36: the configured names, folded once per configuration (see `folded_names`).
    `slow` holds `(name, bounded)` for every distinct folded name, `fast` the search keys
    `(key, bounded)` (the marked name for a bounded name, else the name) without keys that
    another key already implies, `filler` the `_FILLERS` character no name holds, and
    `fast_ok` is false when a name holds `_MARK` or `_SEP` or no filler is free."""
    slow: tuple
    fast: tuple
    fast_ok: bool
    filler: str = ""


@lru_cache(maxsize=32)
def folded_names(texts):
    """P36: fold, dedupe and index `always_human.mention_texts` once (cached per tuple).
    `bounded` is true for an ASCII name, which then needs ASCII word boundaries. A key is
    dropped when another key of the same kind is a proper substring of it: every text the
    longer name matches is then matched by the shorter one, so the answer is unchanged."""
    slow = tuple(dict.fromkeys((name, name.isascii()) for name in map(fold_text, texts)
                               if name))
    filler = next((char for char in _FILLERS if not any(char in name for name, _ in slow)), "")
    fast_ok = bool(filler) and not any(_MARK in name or _SEP in name for name, _ in slow)
    keys = tuple(dict.fromkeys((mark_words(name) if bounded else name, bounded)
                               for name, bounded in slow)) if fast_ok else ()
    fast = tuple(entry for entry in keys
                 if not any(other != entry and other[1] == entry[1] and other[0] in entry[0]
                            for other in keys))
    return FoldedNames(slow, fast, fast_ok, filler)


def _word_char(char):
    return char.isascii() and char.isalnum()


def contains_name(segment, name, bounded):
    """P36: whether the folded `segment` contains the folded `name`. A `bounded` (ASCII)
    name must not touch an ASCII letter or digit on a side where the name itself starts or
    ends with one (`sam` does not match `sample`; it matches `sam,` and `sam` followed by a
    Hangul honorific). Plain substring search (`str.find`), no regex built from
    configuration. The reference rule; `name_mention` uses it only on its slow path."""
    size = len(name)
    position = segment.find(name)
    while position >= 0:
        if not bounded:
            return True
        end = position + size
        left = (position == 0 or not _word_char(name[0])
                or not _word_char(segment[position - 1]))
        right = (end == len(segment) or not _word_char(name[-1])
                 or not _word_char(segment[end]))
        if left and right:
            return True
        position = segment.find(name, position + 1)
    return False


def name_mention(message, config):
    """P36: True when the text, outside Slack entities, contains one of
    `always_human.mention_texts` (compared after `fold_text` on both sides).

    Fast path: the folded parts are joined with `_SEP` (a name never holds it, so no match
    crosses an entity) and each key is one `in` test, on the marked text for bounded names.
    That is a constant number of C-level linear scans per name, whatever the text. A
    `_MARK` or `_SEP` in the folded text is replaced with `names.filler` first. The slow
    path (`contains_name` per part) runs only for a configuration whose names hold `_MARK`
    or `_SEP` (see `FoldedNames.fast_ok`), never because of the text."""
    names = folded_names(tuple(getattr(config.always_human, "mention_texts", ()) or ()))
    text = message.get("text")
    if not names.slow or not isinstance(text, str) or not text:
        return False
    segments = [fold_text(segment) for segment in plain_segments(text)]
    if names.fast_ok:
        joined = _SEP.join(segments)
        if _MARK in joined or joined.count(_SEP) != len(segments) - 1:
            joined = _SEP.join(segment.replace(_MARK, names.filler).replace(_SEP, names.filler)
                               for segment in segments)
        marked = None
        for key, bounded in names.fast:
            if bounded:
                if marked is None:
                    marked = mark_words(joined)
                if key in marked:
                    return True
            elif key in joined:
                return True
        return False
    return any(contains_name(segment, name, bounded)
               for name, bounded in names.slow for segment in segments if segment)


def attention_for(message, config):
    """`owner_mention` for an exact mention of a mention user or the owner, or (P36) a
    configured `always_human.mention_texts` name in the text, else `always_human` for a
    protected author, else None."""
    protected = config.always_human
    attention = "always_human" if message_author(message) in protected.users else None
    if exact_mention(message, config) or name_mention(message, config):
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
