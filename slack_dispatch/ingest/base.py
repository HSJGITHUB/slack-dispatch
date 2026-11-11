"""Sources emit normalized events to a caller-provided callback."""
from collections.abc import Callable
from typing import Any, Protocol

Event = dict[str, Any]
EventHandler = Callable[[Event], None]


class Source(Protocol):
    """start begins intake and invokes on_event with each normalized event.

    stop releases source resources and ends callback delivery; construction does not start intake.
    """

    on_event: EventHandler

    def start(self) -> None: ...

    def stop(self) -> None: ...
