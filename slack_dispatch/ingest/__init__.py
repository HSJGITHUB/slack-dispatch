"""Build configured sources without opening credentials or making requests."""
from dataclasses import replace

from .base import Source
from .slack_reconcile import SlackReconcileSource
from .slack_socket import SlackSocketSource


def build_sources(config, on_event, store) -> list[Source]:
    sources = []
    store.store_body = config.ledger.store_body
    for entry in config.sources:
        if not entry.enabled:
            continue
        entry = replace(entry, store_body=config.ledger.store_body)
        if entry.kind == "slack_socket":
            sources.append(SlackSocketSource(entry, on_event, entry.env_file, entry.app_token_key))
        elif entry.kind == "slack_reconcile":
            sources.append(SlackReconcileSource(entry, on_event, entry.env_file,
                                                 entry.user_token_key, store))
            sources[-1].owner_user = config.always_human.owner_user
        else:
            raise ValueError("Unsupported source kind")
    return sources
