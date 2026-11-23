"""Where the message stores come from.

A backend hands the reader ordinary SQLite files. ``PlainBackend`` reads a
directory of stores that are already readable and does nothing else. Another
backend can be plugged in with ``CW_DB_BACKEND=package.module:ClassName``;
it must offer the same attributes and methods as ``PlainBackend``.
"""
from __future__ import annotations

import importlib
import os
import re
from pathlib import Path

_MESSAGE_SHARD = re.compile(r"^message_(\d+)\.db$")
_MEDIA_SHARD = re.compile(r"(?:^|/)media_(\d+)\.db$")
_ACCOUNT_DIR = re.compile(r"^(.+?)_[0-9a-fA-F]{4}$")


def norm_rel_key(value):
    return str(value or "").replace("\\", "/").lstrip("./")


class DirectQueryUnavailable(RuntimeError):
    """This backend cannot answer SQL without opening a store file."""


class PlainBackend:
    """Readable SQLite stores laid out as ``<root>/<area>/<name>.db``.

    Expected files, all relative to ``CW_PLAIN_DB_DIR``::

        message/message_0.db, message/message_1.db, ...   chat messages
        message/message_resource.db                       media index
        message/media_0.db, ...                           voice blobs
        contact/contact.db                                contacts and groups
        session/session.db                                conversation list
    """

    name = "plain"
    direct_query_enabled = False

    def __init__(self):
        root = str(os.environ.get("CW_PLAIN_DB_DIR", "")).strip()
        if not root:
            raise RuntimeError(
                "CW_PLAIN_DB_DIR is not set; point it at a directory "
                "of readable SQLite stores"
            )
        self.db_dir = Path(root)
        if not self.db_dir.is_dir():
            raise RuntimeError(f"store directory not found: {self.db_dir}")
        self.account_root = self.db_dir.parent
        self._rel_keys = ()
        self._stamps = {}
        self.reload()

    # -- identity ---------------------------------------------------------
    @property
    def account_id(self):
        explicit = str(os.environ.get("CW_ACCOUNT_ID", "")).strip()
        if explicit:
            return explicit
        name = self.account_root.name
        match = _ACCOUNT_DIR.match(name)
        return match.group(1) if match else name

    # -- discovery --------------------------------------------------------
    def reload(self):
        found = []
        for path in self.db_dir.rglob("*.db"):
            if path.is_file():
                found.append(path.relative_to(self.db_dir).as_posix())
        self._rel_keys = tuple(sorted(found))
        self._stamps = {}

    def maybe_refresh(self):
        before = self._rel_keys
        stamps = self._stamps
        self.reload()
        if self._rel_keys == before:
            self._stamps = stamps
            return False
        return True

    def rel_keys(self):
        return list(self._rel_keys)

    def message_shards(self):
        shards = []
        for rel_key in self._rel_keys:
            if not rel_key.startswith("message/"):
                continue
            match = _MESSAGE_SHARD.match(rel_key.rsplit("/", 1)[-1])
            if match:
                shards.append((int(match.group(1)), rel_key))
        return [rel_key for _, rel_key in sorted(shards)]

    def unreadable_message_shards(self):
        return []

    def find_rel_key(self, suffix):
        for rel_key in self._rel_keys:
            if rel_key.endswith(suffix):
                return rel_key
        return None

    def media_shards(self):
        shards = []
        for rel_key in self._rel_keys:
            match = _MEDIA_SHARD.search(rel_key)
            if match:
                shards.append((int(match.group(1)), rel_key))
        return [rel_key for _, rel_key in sorted(shards)]

    # -- access -----------------------------------------------------------
    def _stamp(self, path):
        stamp = []
        for candidate in (path, Path(str(path) + "-wal")):
            try:
                info = candidate.stat()
                stamp.append((info.st_mtime_ns, info.st_size))
            except OSError:
                stamp.append(None)
        return tuple(stamp)

    def path_with_status(self, rel_key):
        """Return ``(path, changed_since_last_call, mode)``."""
        key = norm_rel_key(rel_key)
        path = self.db_dir / Path(key)
        if not path.is_file():
            return None, False, "missing-db"
        stamp = self._stamp(path)
        changed = self._stamps.get(key) != stamp
        self._stamps[key] = stamp
        return str(path), changed, "plain"

    def path(self, rel_key):
        return self.path_with_status(rel_key)[0]

    def has_wal_tail(self, rel_key):
        wal = Path(str(self.db_dir / Path(norm_rel_key(rel_key))) + "-wal")
        try:
            return int(wal.stat().st_size) > 32
        except OSError:
            return False

    def direct_query(self, rel_key, sql, timeout=5.0):
        raise DirectQueryUnavailable(self.name)

    def discard(self):
        dropped = len(self._stamps)
        self._stamps = {}
        return dropped


def load_backend():
    spec = str(os.environ.get("CW_DB_BACKEND", "")).strip()
    if not spec:
        # A deployment may drop a ``db_backend_local.py`` next to this file;
        # its ``Backend`` class is then used without any configuration.
        local_name = f"{__package__}.db_backend_local"
        try:
            local = importlib.import_module(local_name)
        except ModuleNotFoundError as exc:
            if exc.name != local_name:
                raise
            return PlainBackend()
        return local.Backend()
    if spec == "plain":
        return PlainBackend()
    module_name, _, class_name = spec.partition(":")
    if not module_name or not class_name:
        raise RuntimeError(
            "CW_DB_BACKEND must be 'plain' or 'package.module:ClassName'"
        )
    module = importlib.import_module(module_name)
    return getattr(module, class_name)()
