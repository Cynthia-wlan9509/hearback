"""Functions run in child processes by test_ledger (must be importable, hence a module)."""
from __future__ import annotations

from hearback.ledger import Ledger


def bump(path: str, n: int) -> int:
    """Increment data['counter'] n times, each under the ledger lock."""
    ledger = Ledger(path, lock_timeout=60)
    for _ in range(n):
        with ledger.locked():
            data = ledger.load()
            data["counter"] = data.get("counter", 0) + 1
            ledger.save(data)
    return n
