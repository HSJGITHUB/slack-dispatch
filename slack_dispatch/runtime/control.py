"""File flags shared with local operators."""
from datetime import datetime
from pathlib import Path
from uuid import uuid4


class ControlFlags:
    def __init__(self, directory):
        self.directory = Path(directory)

    def should_stop(self):
        return (self.directory / "stop.flag").exists()

    def wake_enabled(self):
        return not (self.directory / "wake.off").exists()

    def acknowledge_stop(self):
        source = self.directory / "stop.flag"
        if not source.exists():
            return None
        done = self.directory / "done"
        done.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S.%f%z")
        target = done / f"{stamp}_{uuid4().hex}_stop.flag"
        try:
            source.rename(target)
        except FileNotFoundError:
            return None
        return target