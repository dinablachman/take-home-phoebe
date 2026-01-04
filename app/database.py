from collections.abc import Iterator, MutableMapping
from typing import TypeVar

K = TypeVar("K")
V = TypeVar("V")


class InMemoryKeyValueDatabase[K, V]:
    """
    Simple in-memory key/value database.
    """

    def __init__(self) -> None:
        self._store: MutableMapping[K, V] = {}

    def put(self, key: K, value: V) -> None:
        self._store[key] = value

    def get(self, key: K) -> V | None:
        return self._store.get(key)

    def delete(self, key: K) -> None:
        self._store.pop(key, None)

    def all(self) -> list[V]:
        return list(self._store.values())

    def clear(self) -> None:
        self._store.clear()

    def __iter__(self) -> Iterator[V]:
        return iter(self._store.values())

    def __len__(self) -> int:
        return len(self._store)

    def claim_shift_if_unclaimed(
        self, key: K, claimer_id: str, claimed_at
    ) -> bool:
        """
        Atomically claim a shift if it's not already claimed.
        Returns True if claim succeeded, False if already claimed.
        """
        value = self._store.get(key)
        if value is None:
            return False
        # Check and set in minimal window - dict access is atomic
        if hasattr(value, "claimed") and not value.claimed:
            value.claimed = True
            if hasattr(value, "claimed_by"):
                value.claimed_by = claimer_id
            if hasattr(value, "claimed_at"):
                value.claimed_at = claimed_at
            self._store[key] = value
            return True
        return False
