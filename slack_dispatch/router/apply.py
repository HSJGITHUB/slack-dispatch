"""Apply decisions exclusively through the ledger contract."""
from dataclasses import dataclass

from .rules import is_reply


@dataclass(frozen=True)
class Outcome:
    state: str
    lane: str | None
    reason: str
    suggested_lane: str | None


def apply_decision(store, message_id, decision, suggester) -> Outcome:
    message = store.get_message(message_id)
    if message is None:
        raise KeyError(message_id)
    if decision.attention is not None or message.get("attention") is not None:
        store.set_attention(message_id, decision.attention)
    if decision.rule == "excluded":
        store.set_state(message_id, "excluded", note=decision.reason)
        return Outcome("excluded", None, decision.reason, None)
    if decision.lane is not None:
        store.set_route(message_id, decision.lane, decision.reason)
        if is_reply(message):
            key = (message["source"], message["channel"], message["thread_ts"])
            if store.lookup_binding(*key) is None:
                store.bind_thread(*key, decision.lane)
        return Outcome("assigned", decision.lane, decision.reason, None)
    store.set_state(message_id, "human_pending", note=decision.reason)
    suggestion = None
    if decision.rule == "none" and suggester is not None and getattr(suggester, "enabled", True):
        suggestion = suggester.suggest(message)
        if suggestion is not None:
            store.set_suggestion(message_id, suggestion.lane, suggestion.reason,
                                 suggestion.classifier_version)
    return Outcome("human_pending", None, decision.reason,
                   suggestion.lane if suggestion else None)
