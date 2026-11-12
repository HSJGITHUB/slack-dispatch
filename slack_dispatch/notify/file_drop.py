"""Atomic, configurable assistant outbox records containing ids only."""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .session_message import summary


class FileDropNotifier:
    def __init__(self, outbox_dir, template):
        self.outbox_dir = Path(outbox_dir)
        self.template = template

    def notify(self, lane, message_ids):
        values = {"lane": lane, "count": len(message_ids), "ids": list(message_ids),
                  "title": f"Lane {lane}", "body": summary(lane, message_ids)}

        def render(value):
            if isinstance(value, dict):
                return {key: render(item) for key, item in value.items()}
            if isinstance(value, list):
                return [render(item) for item in value]
            if isinstance(value, str):
                for key, replacement in values.items():
                    if value == "{" + key + "}":
                        return replacement
                strings = dict(values, ids=", ".join(message_ids))
                return re.sub(r"\{(lane|count|ids|title|body)\}",
                              lambda match: str(strings[match[1]]), value)
            return value

        payload = render(self.template)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + uuid4().hex
        temp = self.outbox_dir / (name + ".tmp")
        with temp.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.outbox_dir / (name + ".json"))
