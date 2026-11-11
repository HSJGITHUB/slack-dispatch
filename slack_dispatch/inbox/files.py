"""Store ledger ids in per-lane JSONL files and serialize file access with a file lock.

ack and done transition ledger state to acked and done, while reroute changes lane
ownership through the single ledger writer before updating inbox delivery.
"""
from pathlib import Path


def append(root: Path, lane: str, message_ids: list[str]) -> None:
    raise NotImplementedError("Inbox append is not implemented")


def read(root: Path, lane: str) -> list[dict]:
    raise NotImplementedError("Inbox read is not implemented")
