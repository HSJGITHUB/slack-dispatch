"""Suggestions do not grant authority to assign a lane."""
from dataclasses import dataclass
from typing import Protocol

from .rules import Message


@dataclass(frozen=True)
class Suggestion:
    lane: str
    reason: str
    classifier_version: str


class Suggester(Protocol):
    """Return a suggest-only lane proposal for a message, or None, without assigning work.

    Self-reported confidence must never serve as grounds for automatic assignment.
    """

    def suggest(self, message: Message) -> Suggestion | None: ...


class NullSuggester:
    def suggest(self, message: Message) -> Suggestion | None:
        return None
