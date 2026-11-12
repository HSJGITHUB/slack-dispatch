"""OS-held locks with a persistent, readable owner PID."""

import errno
import os
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class AlreadyRunning(RuntimeError):
    def __init__(self, pid):
        self.pid = pid
        super().__init__(f"Another instance holds the lock (pid={pid})")


class SingleInstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self._file = None

    def __enter__(self):
        if self._file is not None:
            raise AlreadyRunning(os.getpid())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Byte zero is reserved for Windows locking; PID bytes remain readable.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(fd, "r+b", buffering=0)
        try:
            try:
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                handle.seek(1)
                raw = handle.read().strip()
                pid = int(raw) if raw.isdigit() else None
                raise AlreadyRunning(pid) from exc
            handle.seek(1)
            handle.write(str(os.getpid()).encode("ascii"))
            handle.truncate()
            os.fsync(handle.fileno())
        except BaseException:
            handle.close()
            raise
        self._file = handle
        return self

    def __exit__(self, exc_type, exc, tb):
        handle, self._file = self._file, None
        if handle is not None:
            try:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        # Never unlink a lock file: replacing its inode would split ownership.
