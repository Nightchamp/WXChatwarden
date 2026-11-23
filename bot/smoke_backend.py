"""A tiny stand-in store backend for the smoke tests."""
from __future__ import annotations

from pathlib import Path

from chatwarden.db_backend import DirectQueryUnavailable, PlainBackend


class StubBackend(PlainBackend):
    name = "stub"

    def __init__(self, cache=None, db_dir=None, direct_query=None, shards=()):
        self.cache = cache
        self.db_dir = Path(db_dir) if db_dir is not None else Path("/nonexistent")
        self.account_root = self.db_dir.parent
        self._direct_query = direct_query
        self.direct_query_enabled = direct_query is not None
        self._rel_keys = tuple(shards)
        self._stamps = {}

    def reload(self):
        return None

    def maybe_refresh(self):
        return False

    def path_with_status(self, rel_key):
        with_status = getattr(self.cache, "get_with_status", None)
        if callable(with_status):
            return with_status(rel_key)
        return self.cache.get(rel_key), True, "stub"

    def path(self, rel_key):
        return self.cache.get(rel_key)

    def direct_query(self, rel_key, sql, timeout=5.0):
        if self._direct_query is None:
            raise DirectQueryUnavailable(self.name)
        return self._direct_query(rel_key, sql, timeout=timeout)

    def discard(self):
        return 0
