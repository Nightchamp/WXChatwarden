from __future__ import annotations

import ctypes
import errno
import os
import select
import struct
import time
from pathlib import Path


_IN_MODIFY = 0x00000002
_IN_ATTRIB = 0x00000004
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_IN_Q_OVERFLOW = 0x00004000
_IN_IGNORED = 0x00008000

_WATCH_MASK = (
    _IN_MODIFY
    | _IN_ATTRIB
    | _IN_CLOSE_WRITE
    | _IN_MOVED_TO
    | _IN_CREATE
    | _IN_DELETE
    | _IN_DELETE_SELF
    | _IN_MOVE_SELF
)

_EVENT = struct.Struct("iIII")


def _normalise_rel_key(value):
    return str(value or "").replace("\\", "/").lstrip("./")


class WalChangeWatcher:
    """Small Linux inotify watcher for WeChat DB/WAL changes.

    It intentionally does not copy, transform, or archive any data. The watcher
    only wakes the existing DB-native parser and therefore keeps message
    parsing and routing semantics in one place.
    """

    def __init__(
        self,
        db_dir,
        rel_keys=(),
        settle_seconds=0.08,
    ):
        self.db_dir = Path(db_dir)
        self.settle_seconds = max(0.0, float(settle_seconds))
        self.rel_keys = set()
        self._fd = None
        self._poll = None
        self._watch_by_dir = {}
        self._dir_by_watch = {}
        self._stamps = {}
        self.force_full_scan = False
        self.error = None

        try:
            libc = ctypes.CDLL(None, use_errno=True)
            init = libc.inotify_init1
            init.argtypes = [ctypes.c_int]
            init.restype = ctypes.c_int
            add = libc.inotify_add_watch
            add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            add.restype = ctypes.c_int

            fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
            if fd < 0:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err))

            self._add_watch = add
            self._fd = fd
            self._poll = select.poll()
            self._poll.register(fd, select.POLLIN)
            self.update_rel_keys(rel_keys)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.close()

    @property
    def available(self):
        return self._fd is not None

    @property
    def watched_dirs(self):
        return tuple(sorted(str(path) for path in self._watch_by_dir))

    def _add_dir(self, directory):
        directory = Path(directory)
        if directory in self._watch_by_dir or not directory.is_dir():
            return
        wd = self._add_watch(
            self._fd,
            os.fsencode(str(directory)),
            _WATCH_MASK,
        )
        if wd < 0:
            err = ctypes.get_errno()
            if err in {errno.ENOENT, errno.ENOTDIR}:
                return
            raise OSError(err, os.strerror(err), str(directory))
        self._watch_by_dir[directory] = wd
        self._dir_by_watch[wd] = directory

    def _stamp(self, rel_key):
        db_path = self.db_dir / Path(rel_key)
        wal_path = Path(str(db_path) + "-wal")
        material_paths = (
            Path(str(db_path) + "-first.material"),
            Path(str(db_path) + "-incremental.material"),
            Path(str(db_path) + "-last.material"),
        )

        def one(path):
            try:
                stat = path.stat()
                return (int(stat.st_mtime_ns), int(stat.st_size))
            except OSError:
                return None

        return (
            one(db_path),
            one(wal_path),
            *(one(path) for path in material_paths),
        )

    def update_rel_keys(self, rel_keys):
        for rel_key in rel_keys or ():
            norm = _normalise_rel_key(rel_key)
            if not norm:
                continue
            is_new = norm not in self.rel_keys
            self.rel_keys.add(norm)
            if is_new:
                self._stamps[norm] = self._stamp(norm)
            if self.available:
                try:
                    self._add_dir(self.db_dir / Path(norm).parent)
                except Exception as exc:
                    self.error = f"{type(exc).__name__}: {exc}"
                    self.force_full_scan = True

        # The message directory must also be watched for a newly-created
        # numeric shard whose key appears between periodic key refreshes.
        if self.available:
            try:
                self._add_dir(self.db_dir / "message")
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                self.force_full_scan = True

    def _rel_key_for_event(self, directory, name):
        if not name:
            return None
        if name.endswith(".db-wal"):
            name = name[:-4]
        elif not name.endswith(".db"):
            material_suffix = next(
                (
                    suffix
                    for suffix in (
                        "-first.material",
                        "-incremental.material",
                        "-last.material",
                    )
                    if name.endswith(suffix)
                ),
                None,
            )
            if material_suffix is None:
                return None
            name = name[: -len(material_suffix)]
            if not name.endswith(".db"):
                return None

        try:
            parent = directory.relative_to(self.db_dir)
        except ValueError:
            return None

        rel_key = _normalise_rel_key(parent / name)
        if rel_key in self.rel_keys:
            return rel_key
        if (
            rel_key.startswith("message/message_")
            and rel_key.endswith(".db")
        ):
            return rel_key
        return None

    def _drain(self):
        changed = set()
        if not self.available:
            return changed

        while True:
            try:
                payload = os.read(self._fd, 65536)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno == errno.EAGAIN:
                    break
                self.error = f"{type(exc).__name__}: {exc}"
                self.force_full_scan = True
                self.close()
                break

            if not payload:
                break

            offset = 0
            size = len(payload)
            while offset + _EVENT.size <= size:
                wd, mask, _cookie, name_len = _EVENT.unpack_from(
                    payload,
                    offset,
                )
                offset += _EVENT.size
                raw_name = payload[offset : offset + name_len]
                offset += name_len

                if mask & _IN_Q_OVERFLOW:
                    self.force_full_scan = True
                    continue
                if mask & _IN_IGNORED:
                    directory = self._dir_by_watch.pop(wd, None)
                    if directory is not None:
                        self._watch_by_dir.pop(directory, None)
                    self.force_full_scan = True
                    continue

                directory = self._dir_by_watch.get(wd)
                if directory is None:
                    continue
                name = os.fsdecode(raw_name.split(b"\x00", 1)[0])
                rel_key = self._rel_key_for_event(directory, name)
                if rel_key:
                    changed.add(rel_key)

        return changed

    def scan_for_changes(self):
        """Cheap stat fallback for an inotify notification that was missed."""
        changed = set()
        for rel_key in tuple(self.rel_keys):
            current = self._stamp(rel_key)
            previous = self._stamps.get(rel_key)
            if previous is None:
                self._stamps[rel_key] = current
                continue
            if current != previous:
                changed.add(rel_key)
                self._stamps[rel_key] = current
        return changed

    def wait(self, timeout):
        timeout = max(0.0, float(timeout))
        if not self.available:
            if timeout:
                time.sleep(timeout)
            self.force_full_scan = True
            return set()

        ready = self._poll.poll(int(timeout * 1000))
        if not ready:
            return set()
        if any(
            mask & (select.POLLERR | select.POLLHUP | select.POLLNVAL)
            for _fd, mask in ready
        ):
            self.error = "inotify poll failed"
            self.force_full_scan = True
            self.close()
            return set()

        changed = self._drain()
        if self.settle_seconds:
            time.sleep(self.settle_seconds)
            changed.update(self._drain())
        for rel_key in changed:
            self._stamps[rel_key] = self._stamp(rel_key)
        return changed

    def consume_force_full_scan(self):
        value = bool(self.force_full_scan)
        self.force_full_scan = False
        return value

    def close(self):
        fd = self._fd
        self._fd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        self._poll = None
        self._watch_by_dir.clear()
        self._dir_by_watch.clear()
        self._stamps.clear()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
