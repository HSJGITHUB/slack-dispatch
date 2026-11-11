"""First-match orchestration; concrete rule evaluation remains unimplemented.

Production order: ThreadInherit, ExplicitTag, ChannelDefault, PersonChannel.
An unmatched result must enter the human queue, never an implicit lane.
"""
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

Message = dict[str, Any]


@dataclass(frozen=True)
class Route:
    lane: str
    reason: str


class Rule(Protocol):
    def match(self, message: Message) -> Route | None: ...


class ThreadInherit:
    """Evaluate a message with thread bindings and return a lane id in Route or None.

    Evaluation has no side effects; only the first matching rule applies.
    """

    def match(self, message: Message) -> Route | None:
        raise NotImplementedError("Thread binding lookup is not implemented")


class ExplicitTag:
    """Evaluate a message with the configured tag map and return a lane id in Route or None.

    Evaluation has no side effects; only the first matching rule applies.
    """

    def match(self, message: Message) -> Route | None:
        raise NotImplementedError("Explicit tag lookup is not implemented")


class ChannelDefault:
    """Evaluate a message with channel defaults and return a lane id in Route or None.

    Evaluation has no side effects; only the first matching rule applies.
    """

    def match(self, message: Message) -> Route | None:
        raise NotImplementedError("Channel default lookup is not implemented")


class PersonChannel:
    """Evaluate a message with person-channel rules and return a lane id in Route or None.

    Evaluation has no side effects; only the first matching rule applies.
    """

    def match(self, message: Message) -> Route | None:
        raise NotImplementedError("Person and channel lookup is not implemented")


def first_match(message: Message, rules: Iterable[Rule]) -> Route | None:
    for rule in rules:
        result = rule.match(message)
        if result is not None:
            return result
    return None
