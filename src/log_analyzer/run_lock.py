"""POSIX advisory lock used to prevent overlapping batch executions."""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from .errors import AlreadyRunningError, UnsupportedPlatformError

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    _fcntl = None


class RunLock:
    """Hold a non-blocking flock for the lifetime of one process run.

    The lock file is deliberately retained after release. Kernel lock ownership,
    not file existence or the diagnostic metadata, determines whether a run is
    active.
    """

    def __init__(self, path: str | Path, config_path: str | Path) -> None:
        self.path = Path(path)
        self.config_path = Path(config_path)
        self._stream: IO[str] | None = None
        self.metadata: dict[str, Any] | None = None

    @property
    def acquired(self) -> bool:
        return self._stream is not None

    @staticmethod
    def supported() -> bool:
        return os.name == "posix" and _fcntl is not None

    def acquire(self) -> "RunLock":
        if self.acquired:
            return self
        if not self.supported():
            raise UnsupportedPlatformError(
                "run locking requires POSIX fcntl.flock; it is not available "
                f"on {os.name!r}"
            )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o640)
        try:
            os.fchmod(descriptor, 0o640)
            stream = os.fdopen(descriptor, "r+", encoding="utf-8")
        except Exception:
            os.close(descriptor)
            raise
        try:
            assert _fcntl is not None
            _fcntl.flock(stream.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            owner = self._read_metadata(stream)
            stream.close()
            if isinstance(exc, BlockingIOError) or getattr(exc, "errno", None) in {
                11,
                13,
            }:
                raise AlreadyRunningError(owner) from exc
            raise

        metadata = {
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "config_path": str(self.config_path),
            "hostname": socket.gethostname(),
        }
        try:
            stream.seek(0)
            stream.truncate()
            json.dump(metadata, stream, ensure_ascii=True, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except Exception:
            assert _fcntl is not None
            _fcntl.flock(stream.fileno(), _fcntl.LOCK_UN)
            stream.close()
            raise
        self._stream = stream
        self.metadata = metadata
        return self

    @staticmethod
    def _read_metadata(stream: IO[str]) -> dict[str, Any]:
        try:
            stream.seek(0)
            value = json.load(stream)
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    def release(self) -> None:
        stream, self._stream = self._stream, None
        self.metadata = None
        if stream is None:
            return
        try:
            assert _fcntl is not None
            _fcntl.flock(stream.fileno(), _fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self) -> "RunLock":
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()
