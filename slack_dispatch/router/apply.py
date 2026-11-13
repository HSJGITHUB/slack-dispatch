"""Apply decisions exclusively through the ledger contract."""
from dataclasses import dataclass
import logging

from .rules import is_dm, is_reply

LOG = logging.getLogger(__name__)
DEFAULT_MIN_EVIDENCE = 2


def scope_key(message):
    """The (kind, key) a routing event is evidence for: dm or channel, by conversation."""
    channel = message.get("channel")
    if not channel:
        return None
    return ("dm" if is_dm(message) else "channel"), channel


def record_scope_proposal(store, message, lane, source, *, min_evidence=None):
    """Count one learning event (P13): a reroute, or a human assignment of a queued item.

    Writes a `scope_proposals` row only; routing never reads proposals. Stores without
    `record_scope_evidence` (test fakes) skip it. Learning is advisory: a failure is
    logged by class name and never fails the action. Returns the proposal row or None.
    """
    record = getattr(store, "record_scope_evidence", None)
    key = scope_key(message)
    if not callable(record) or key is None or not lane:
        return None
    try:
        return record(*key, lane, source,
                      min_evidence=DEFAULT_MIN_EVIDENCE if min_evidence is None else min_evidence)
    except Exception as error:
        LOG.warning("Scope learning skipped class=%s", type(error).__name__)
        return None


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
