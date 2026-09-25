import fcntl
import os
from pathlib import Path


class ScoutAlreadyRunning(RuntimeError):
    pass


class ScoutLock:
    """An exclusive, non-blocking flock.

    The OS releases it if the process dies, so it never goes stale.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ScoutAlreadyRunning(f"another scout is running (lock: {self.path})") from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> ScoutLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
