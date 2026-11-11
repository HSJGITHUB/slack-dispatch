"""open establishes the SQLite connection; migrate applies versioned schema changes before intake.

A single writer owns ledger mutations and transactional deduplication; construction
only records the path and performs no database access.
"""
from pathlib import Path


class LedgerStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def open(self) -> None:
        raise NotImplementedError("Ledger open is not implemented")

    def migrate(self) -> None:
        raise NotImplementedError("Ledger migrations are not implemented")
