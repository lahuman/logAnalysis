"""Read timestamped text logs and multiline Java traces from a private file snapshot."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
import hashlib
import os
import re
import stat
import tempfile
from typing import BinaryIO
from uuid import uuid4

from ..config import FileSourceConfig
from ..models import ErrorEvent, ErrorPage, ErrorQuery


class FileSourceError(RuntimeError):
    """A diagnostic that never includes the input log text."""


class FileErrorSource:
    def __init__(self, *, config: FileSourceConfig, max_text_characters: int = 100_000) -> None:
        if max_text_characters < 1:
            raise ValueError("a positive text limit is required")
        self._config = config
        self._max_text = max_text_characters
        self._header = re.compile(config.header_pattern)
        offset = config.timestamp_timezone
        minutes = (int(offset[1:3]) * 60 + int(offset[4:])) * (-1 if offset[0] == "-" else 1)
        self._timezone = timezone(timedelta(minutes=minutes))
        self._snapshot: BinaryIO | None = None
        self._query: ErrorQuery | None = None
        self._token: str | None = None

    def _capture(self) -> BinaryIO:
        snapshot = tempfile.TemporaryFile(mode="w+b")
        try:
            if not self._config.path.is_file():
                raise FileSourceError("log input must be a readable regular file")
            with self._config.path.open("rb") as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > self._config.max_file_bytes:
                    raise FileSourceError("log input is not a regular file or exceeds max_file_bytes")
                remaining = before.st_size
                while remaining:
                    chunk = source.read(min(1_048_576, remaining))
                    if not chunk:
                        raise FileSourceError("log file changed while taking a snapshot; retry with a completed copy")
                    snapshot.write(chunk)
                    remaining -= len(chunk)
                after = os.fstat(source.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns
                ):
                    raise FileSourceError("log file changed while taking a snapshot; retry with a completed copy")
            snapshot.seek(0)
            return snapshot
        except BaseException:
            snapshot.close()
            raise

    def _record(self) -> ErrorEvent | None:
        stream = self._snapshot
        assert stream is not None
        start = stream.tell()
        raw = bytearray()
        header = None
        while True:
            position = stream.tell()
            line = stream.readline(self._config.max_record_bytes + 1)
            if not line:
                break
            if len(line) > self._config.max_record_bytes:
                raise FileSourceError(f"log line exceeds max_record_bytes at byte {position}")
            text = line.decode(self._config.encoding)
            if position == 0:
                text = text.removeprefix("\ufeff")
            match = self._header.match(text.rstrip("\r\n"))
            if header is not None and match is not None:
                stream.seek(position)
                break
            if header is None:
                if not text.strip():
                    start = stream.tell()
                    continue
                if match is None:
                    raise FileSourceError(f"log header does not match header_pattern at byte {position}")
                header = match
            raw.extend(line)
            if len(raw) > self._config.max_record_bytes:
                raise FileSourceError(f"log record exceeds max_record_bytes at byte {start}")
        if header is None:
            return None
        try:
            timestamp = datetime.fromisoformat(header.group("timestamp"))
            if timestamp.utcoffset() is None:
                timestamp = timestamp.replace(tzinfo=self._timezone)
            timestamp = timestamp.astimezone(UTC)
            severity = header.group("severity").strip().upper()
            message = header.group("message")
            if not severity or message is None:
                raise ValueError("missing header fields")
        except (ValueError, TypeError, AttributeError):
            raise FileSourceError(f"invalid timestamp or log header fields at byte {start}") from None
        # Offsets distinguish identical occurrences; the digest prevents same-offset replacement collisions.
        identity = hashlib.sha256(str(start).encode("ascii") + b"\0" + raw).hexdigest()
        text = raw.decode(self._config.encoding).removeprefix("\ufeff")
        return ErrorEvent(
            source_name=self._config.name, event_id=identity, occurred_at=timestamp,
            service=self._config.service, severity=severity, message=message[:self._max_text],
            raw_log=text[:self._max_text], stack_trace=text[:self._max_text], language_hint="java",
        )

    async def fetch(self, query: ErrorQuery, cursor: str | None = None) -> ErrorPage:
        if cursor is not None and (cursor != self._token or query != self._query or self._snapshot is None):
            raise FileSourceError("file page cursor is stale or belongs to a different query")
        try:
            if cursor is None:
                await self.close()
                self._snapshot = self._capture()
                self._query = query
            events = []
            while len(events) < query.limit:
                event = self._record()
                if event is None:
                    await self.close()
                    return ErrorPage(events=tuple(events))
                if query.severities and event.severity not in query.severities:
                    continue
                if self._config.filter_time_window and not query.started_at <= event.occurred_at <= query.ended_at:
                    continue
                events.append(event)
            self._token = uuid4().hex
            return ErrorPage(events=tuple(events), next_cursor=self._token)
        except BaseException as exc:
            await self.close()
            if isinstance(exc, FileSourceError) or not isinstance(exc, Exception):
                raise
            raise FileSourceError("local log read failed; check file access and encoding") from None

    async def healthcheck(self) -> None:
        # Use a separate reader so healthcheck cannot consume an active paging cursor.
        reader = FileErrorSource(config=self._config, max_text_characters=self._max_text)
        try:
            reader._snapshot = reader._capture()
            if reader._record() is None:
                raise FileSourceError("local log file contains no records")
        except (OSError, UnicodeError):
            raise FileSourceError("local log check failed; check file access and encoding") from None
        finally:
            await reader.close()

    async def close(self) -> None:
        snapshot, self._snapshot = self._snapshot, None
        self._token = None
        self._query = None
        if snapshot is not None:
            snapshot.close()
