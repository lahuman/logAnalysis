"""SQLite-backed idempotency, checkpoint, retry, and retention state."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Iterator

from .errors import StorageError
from .models import ErrorEvent


class JobStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    PARSED = "PARSED"
    SOURCE_RESOLVED = "SOURCE_RESOLVED"
    ANALYZING = "ANALYZING"
    COMPLETED = "COMPLETED"
    NO_SOURCE = "NO_SOURCE"
    RETRY_WAIT = "RETRY_WAIT"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"


TERMINAL_STATUSES = (
    JobStatus.COMPLETED,
    JobStatus.NO_SOURCE,
    JobStatus.PERMANENT_FAILURE,
)


@dataclass(frozen=True, slots=True)
class Checkpoint:
    source_name: str
    cursor: str | None
    ended_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AnalysisJob:
    source_name: str
    event_id: str
    service: str
    occurred_at: datetime
    fingerprint: str
    git_commit: str | None
    status: JobStatus
    attempts: int
    next_retry_at: datetime | None
    last_error: str | None
    report_path: str | None
    request_json: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AnalysisCacheEntry:
    fingerprint: str
    git_commit: str
    model: str
    prompt_version: str
    analyzer_version: str
    analysis_json: str
    report_path: str | None
    created_at: datetime
    last_used_at: datetime


@dataclass(frozen=True, slots=True)
class RetentionResult:
    deleted_jobs: int
    deleted_cache_entries: int
    report_paths: tuple[str, ...]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _serialize_time(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="microseconds")


def _deserialize_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def _validate_request_json(value: str) -> str:
    if len(value.encode("utf-8")) > 262_144:
        raise ValueError("redacted retry request exceeds 262144 bytes")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("retry request must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("retry request must be a JSON object")
    return value


class SQLiteStateStore:
    """Small synchronous state store intended for one batch process."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        if busy_timeout_seconds <= 0:
            raise ValueError("busy_timeout_seconds must be greater than zero")
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        self._mutex = threading.RLock()
        self._closed = False
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connection = sqlite3.connect(
                str(self.path),
                timeout=busy_timeout_seconds,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute(
                f"PRAGMA busy_timeout = {int(busy_timeout_seconds * 1000)}"
            )
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._initialize_schema()
        except (OSError, sqlite3.Error) as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise StorageError(f"cannot initialize SQLite state at {self.path}: {exc}") from exc

    def _initialize_schema(self) -> None:
        with self._mutex:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO schema_meta(key, value) VALUES ('schema_version', '1')
                    ON CONFLICT(key) DO NOTHING;

                CREATE TABLE IF NOT EXISTS collection_checkpoint (
                    source_name TEXT PRIMARY KEY,
                    cursor TEXT,
                    ended_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS analysis_job (
                    source_name TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    service TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    git_commit TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    next_retry_at TEXT,
                    last_error TEXT,
                    report_path TEXT,
                    request_json TEXT CHECK (
                        request_json IS NULL OR length(request_json) <= 262144
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source_name, event_id)
                );
                CREATE INDEX IF NOT EXISTS analysis_job_retry_idx
                    ON analysis_job(status, next_retry_at);
                CREATE INDEX IF NOT EXISTS analysis_job_updated_idx
                    ON analysis_job(updated_at);

                CREATE TABLE IF NOT EXISTS analysis_cache (
                    fingerprint TEXT NOT NULL,
                    git_commit TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    analyzer_version TEXT NOT NULL,
                    analysis_json TEXT NOT NULL,
                    report_path TEXT,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    PRIMARY KEY (
                        fingerprint, git_commit, model,
                        prompt_version, analyzer_version
                    )
                );
                CREATE INDEX IF NOT EXISTS analysis_cache_used_idx
                    ON analysis_cache(last_used_at);

                CREATE TABLE IF NOT EXISTS report_cleanup_queue (
                    report_path TEXT PRIMARY KEY,
                    queued_at TEXT NOT NULL
                );
                """
            )
            columns = {
                row[1]
                for row in self._connection.execute(
                    "PRAGMA table_info(analysis_job)"
                )
            }
            if "request_json" not in columns:
                self._connection.execute(
                    "ALTER TABLE analysis_job ADD COLUMN request_json TEXT"
                )

    def _ensure_open(self) -> None:
        if self._closed:
            raise StorageError("SQLite state store is closed")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._mutex:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def close(self) -> None:
        with self._mutex:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> "SQLiteStateStore":
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def healthcheck(self) -> None:
        try:
            with self._transaction() as connection:
                result = connection.execute("PRAGMA quick_check").fetchone()
                if result is None or result[0] != "ok":
                    raise StorageError(f"SQLite integrity check failed: {result!r}")
                connection.execute(
                    "UPDATE schema_meta SET value = value WHERE key = 'schema_version'"
                )
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError(f"SQLite healthcheck failed: {exc}") from exc

    def register_event(
        self,
        event: ErrorEvent,
        fingerprint: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Insert an event reference without persisting its raw log."""

        if not fingerprint:
            raise ValueError("fingerprint must not be empty")
        timestamp = _serialize_time(now or _now())
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO analysis_job(
                        source_name, event_id, service, occurred_at, fingerprint,
                        git_commit, status, attempts, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    ON CONFLICT(source_name, event_id) DO NOTHING
                    """,
                    (
                        event.source_name,
                        event.event_id,
                        event.service,
                        _serialize_time(event.occurred_at),
                        fingerprint,
                        event.git_commit,
                        JobStatus.DISCOVERED.value,
                        timestamp,
                        timestamp,
                    ),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise StorageError(f"cannot register event {event.event_id}: {exc}") from exc

    record_occurrence = register_event

    def get_job(self, source_name: str, event_id: str) -> AnalysisJob | None:
        with self._mutex:
            self._ensure_open()
            try:
                row = self._connection.execute(
                    """
                    SELECT source_name, event_id, service, occurred_at, fingerprint,
                           git_commit, status, attempts, next_retry_at, last_error,
                           report_path, request_json, created_at, updated_at
                    FROM analysis_job
                    WHERE source_name = ? AND event_id = ?
                    """,
                    (source_name, event_id),
                ).fetchone()
            except sqlite3.Error as exc:
                raise StorageError(f"cannot read job {event_id}: {exc}") from exc
        return self._job_from_row(row) if row is not None else None

    def set_status(
        self,
        source_name: str,
        event_id: str,
        status: JobStatus | str,
        *,
        error: str | None = None,
        report_path: str | Path | None = None,
        request_json: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        normalized_status = JobStatus(status)
        timestamp = _serialize_time(now or _now())
        safe_error = error[:2_000] if error else None
        safe_request = (
            _validate_request_json(request_json) if request_json is not None else None
        )
        clear_request = normalized_status in TERMINAL_STATUSES
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    """
                    UPDATE analysis_job
                    SET status = ?, next_retry_at = NULL, last_error = ?,
                        report_path = COALESCE(?, report_path),
                        request_json = CASE
                            WHEN ? THEN NULL
                            ELSE COALESCE(?, request_json)
                        END,
                        updated_at = ?
                    WHERE source_name = ? AND event_id = ?
                    """,
                    (
                        normalized_status.value,
                        safe_error,
                        str(report_path) if report_path is not None else None,
                        clear_request,
                        safe_request,
                        timestamp,
                        source_name,
                        event_id,
                    ),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise StorageError(f"cannot update job {event_id}: {exc}") from exc

    def set_resolved_git_commit(
        self,
        source_name: str,
        event_id: str,
        git_commit: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Persist the immutable source revision selected for this analysis job."""

        git_commit = git_commit.strip()
        if not git_commit:
            raise ValueError("git_commit must not be empty")
        timestamp = _serialize_time(now or _now())
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    """
                    UPDATE analysis_job
                    SET git_commit = ?, updated_at = ?
                    WHERE source_name = ? AND event_id = ?
                      AND (git_commit IS NULL OR git_commit = ?)
                    """,
                    (
                        git_commit,
                        timestamp,
                        source_name,
                        event_id,
                        git_commit,
                    ),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise StorageError(
                f"cannot persist resolved Git commit for {event_id}: {exc}"
            ) from exc

    def mark_retry(
        self,
        source_name: str,
        event_id: str,
        error: str,
        *,
        request_json: str | None = None,
        now: datetime | None = None,
        base_delay_seconds: int = 60,
        max_attempts: int = 5,
        max_delay_seconds: int = 3_600,
    ) -> JobStatus:
        if base_delay_seconds <= 0 or max_attempts <= 0 or max_delay_seconds <= 0:
            raise ValueError("retry limits must be greater than zero")
        current_time = _as_utc(now or _now())
        safe_request = (
            _validate_request_json(request_json) if request_json is not None else None
        )
        try:
            with self._transaction() as connection:
                row = connection.execute(
                    """
                    SELECT attempts, request_json FROM analysis_job
                    WHERE source_name = ? AND event_id = ?
                    """,
                    (source_name, event_id),
                ).fetchone()
                if row is None:
                    raise StorageError(f"cannot retry unknown event {event_id}")
                attempts = int(row["attempts"]) + 1
                if attempts >= max_attempts:
                    status = JobStatus.PERMANENT_FAILURE
                    retry_at = None
                else:
                    status = JobStatus.RETRY_WAIT
                    if safe_request is None and row["request_json"] is None:
                        raise ValueError(
                            "a redacted request_json is required for retry"
                        )
                    delay = min(
                        base_delay_seconds * (2 ** (attempts - 1)),
                        max_delay_seconds,
                    )
                    retry_at = _serialize_time(
                        current_time + timedelta(seconds=delay)
                    )
                connection.execute(
                    """
                    UPDATE analysis_job
                    SET status = ?, attempts = ?, next_retry_at = ?,
                        last_error = ?,
                        request_json = CASE
                            WHEN ? THEN NULL
                            ELSE COALESCE(?, request_json)
                        END,
                        updated_at = ?
                    WHERE source_name = ? AND event_id = ?
                    """,
                    (
                        status.value,
                        attempts,
                        retry_at,
                        error[:2_000],
                        status is JobStatus.PERMANENT_FAILURE,
                        safe_request,
                        _serialize_time(current_time),
                        source_name,
                        event_id,
                    ),
                )
                return status
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError(f"cannot schedule retry for {event_id}: {exc}") from exc

    def due_retries(
        self,
        *,
        now: datetime | None = None,
        limit: int = 500,
    ) -> tuple[AnalysisJob, ...]:
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        with self._mutex:
            self._ensure_open()
            try:
                rows = self._connection.execute(
                    """
                    SELECT source_name, event_id, service, occurred_at, fingerprint,
                           git_commit, status, attempts, next_retry_at, last_error,
                           report_path, request_json, created_at, updated_at
                    FROM analysis_job
                    WHERE status = ? AND next_retry_at <= ?
                    ORDER BY next_retry_at, source_name, event_id
                    LIMIT ?
                    """,
                    (
                        JobStatus.RETRY_WAIT.value,
                        _serialize_time(now or _now()),
                        limit,
                    ),
                ).fetchall()
            except sqlite3.Error as exc:
                raise StorageError(f"cannot read retry queue: {exc}") from exc
        return tuple(self._job_from_row(row) for row in rows)

    jobs_due_for_retry = due_retries

    def recover_interrupted_analysis(self, *, now: datetime | None = None) -> int:
        """Make crash-stranded ANALYZING jobs immediately retryable."""

        timestamp = _serialize_time(now or _now())
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    """
                    UPDATE analysis_job
                    SET status = ?, next_retry_at = ?, updated_at = ?
                    WHERE status = ? AND request_json IS NOT NULL
                    """,
                    (
                        JobStatus.RETRY_WAIT.value,
                        timestamp,
                        timestamp,
                        JobStatus.ANALYZING.value,
                    ),
                )
                return max(cursor.rowcount, 0)
        except sqlite3.Error as exc:
            raise StorageError("cannot recover interrupted analysis jobs") from exc

    def save_retry_context(
        self,
        source_name: str,
        event_id: str,
        request_json: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Persist only an already-redacted, bounded analyzer request."""

        safe_request = _validate_request_json(request_json)
        try:
            with self._transaction() as connection:
                cursor = connection.execute(
                    """
                    UPDATE analysis_job
                    SET request_json = ?, updated_at = ?
                    WHERE source_name = ? AND event_id = ?
                    """,
                    (
                        safe_request,
                        _serialize_time(now or _now()),
                        source_name,
                        event_id,
                    ),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise StorageError(
                f"cannot save retry context for {event_id}: {exc}"
            ) from exc

    def save_checkpoint(
        self,
        source_name: str,
        cursor: str | None,
        ended_at: datetime,
        *,
        now: datetime | None = None,
    ) -> None:
        if not source_name.strip():
            raise ValueError("source_name must not be empty")
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO collection_checkpoint(
                        source_name, cursor, ended_at, updated_at
                    )
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(source_name) DO UPDATE SET
                        cursor = excluded.cursor,
                        ended_at = excluded.ended_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        source_name,
                        cursor,
                        _serialize_time(ended_at),
                        _serialize_time(now or _now()),
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"cannot save checkpoint for {source_name}: {exc}") from exc

    def get_checkpoint(self, source_name: str) -> Checkpoint | None:
        with self._mutex:
            self._ensure_open()
            try:
                row = self._connection.execute(
                    """
                    SELECT source_name, cursor, ended_at, updated_at
                    FROM collection_checkpoint WHERE source_name = ?
                    """,
                    (source_name,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise StorageError(
                    f"cannot read checkpoint for {source_name}: {exc}"
                ) from exc
        if row is None:
            return None
        ended_at = _deserialize_time(row["ended_at"])
        updated_at = _deserialize_time(row["updated_at"])
        assert ended_at is not None and updated_at is not None
        return Checkpoint(
            source_name=row["source_name"],
            cursor=row["cursor"],
            ended_at=ended_at,
            updated_at=updated_at,
        )

    load_checkpoint = get_checkpoint

    def save_analysis_cache(
        self,
        *,
        fingerprint: str,
        git_commit: str,
        model: str,
        prompt_version: str,
        analyzer_version: str,
        analysis_json: str,
        report_path: str | Path | None = None,
        now: datetime | None = None,
    ) -> None:
        identity = (
            fingerprint,
            git_commit,
            model,
            prompt_version,
            analyzer_version,
        )
        if any(not part for part in identity):
            raise ValueError("all analysis cache identity fields must not be empty")
        timestamp = _serialize_time(now or _now())
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO analysis_cache(
                        fingerprint, git_commit, model, prompt_version,
                        analyzer_version, analysis_json, report_path,
                        created_at, last_used_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(
                        fingerprint, git_commit, model,
                        prompt_version, analyzer_version
                    ) DO UPDATE SET
                        analysis_json = excluded.analysis_json,
                        report_path = excluded.report_path,
                        last_used_at = excluded.last_used_at
                    """,
                    (
                        *identity,
                        analysis_json,
                        str(report_path) if report_path is not None else None,
                        timestamp,
                        timestamp,
                    ),
                )
        except sqlite3.Error as exc:
            raise StorageError(f"cannot save analysis cache: {exc}") from exc

    def get_analysis_cache(
        self,
        *,
        fingerprint: str,
        git_commit: str | None,
        model: str,
        prompt_version: str,
        analyzer_version: str,
    ) -> AnalysisCacheEntry | None:
        if not git_commit:
            return None
        with self._mutex:
            self._ensure_open()
            try:
                row = self._connection.execute(
                    """
                    SELECT fingerprint, git_commit, model, prompt_version,
                           analyzer_version, analysis_json, report_path,
                           created_at, last_used_at
                    FROM analysis_cache
                    WHERE fingerprint = ? AND git_commit = ? AND model = ?
                      AND prompt_version = ? AND analyzer_version = ?
                    """,
                    (
                        fingerprint,
                        git_commit,
                        model,
                        prompt_version,
                        analyzer_version,
                    ),
                ).fetchone()
            except sqlite3.Error as exc:
                raise StorageError(f"cannot read analysis cache: {exc}") from exc
        if row is None:
            return None
        created_at = _deserialize_time(row["created_at"])
        last_used_at = _deserialize_time(row["last_used_at"])
        assert created_at is not None and last_used_at is not None
        return AnalysisCacheEntry(
            fingerprint=row["fingerprint"],
            git_commit=row["git_commit"],
            model=row["model"],
            prompt_version=row["prompt_version"],
            analyzer_version=row["analyzer_version"],
            analysis_json=row["analysis_json"],
            report_path=row["report_path"],
            created_at=created_at,
            last_used_at=last_used_at,
        )

    def has_valid_analysis(
        self,
        fingerprint: str,
        git_commit: str | None,
        model: str,
        prompt_version: str,
        analyzer_version: str,
    ) -> bool:
        return (
            self.get_analysis_cache(
                fingerprint=fingerprint,
                git_commit=git_commit,
                model=model,
                prompt_version=prompt_version,
                analyzer_version=analyzer_version,
            )
            is not None
        )

    def purge_older_than(self, cutoff: datetime) -> RetentionResult:
        cutoff_value = _serialize_time(cutoff)
        terminal_values = tuple(status.value for status in TERMINAL_STATUSES)
        placeholders = ", ".join("?" for _ in terminal_values)
        try:
            with self._transaction() as connection:
                job_rows = connection.execute(
                    f"""
                    SELECT report_path FROM analysis_job
                    WHERE updated_at < ? AND status IN ({placeholders})
                    """,
                    (cutoff_value, *terminal_values),
                ).fetchall()
                job_cursor = connection.execute(
                    f"""
                    DELETE FROM analysis_job
                    WHERE updated_at < ? AND status IN ({placeholders})
                    """,
                    (cutoff_value, *terminal_values),
                )
                cache_rows = connection.execute(
                    """
                    SELECT report_path FROM analysis_cache
                    WHERE last_used_at < ?
                    """,
                    (cutoff_value,),
                ).fetchall()
                cache_cursor = connection.execute(
                    "DELETE FROM analysis_cache WHERE last_used_at < ?",
                    (cutoff_value,),
                )
                candidate_paths = {
                    row["report_path"]
                    for row in (*job_rows, *cache_rows)
                    if row["report_path"] is not None
                }
                for path in candidate_paths:
                    still_referenced = connection.execute(
                        """
                        SELECT EXISTS(
                            SELECT 1 FROM analysis_job WHERE report_path = ?
                            UNION ALL
                            SELECT 1 FROM analysis_cache WHERE report_path = ?
                        )
                        """,
                        (path, path),
                    ).fetchone()[0]
                    if not still_referenced:
                        connection.execute(
                            """
                            INSERT INTO report_cleanup_queue(report_path, queued_at)
                            VALUES (?, ?)
                            ON CONFLICT(report_path) DO NOTHING
                            """,
                            (path, _serialize_time(_now())),
                        )
                queued_rows = connection.execute(
                    """
                    SELECT queue.report_path
                    FROM report_cleanup_queue AS queue
                    WHERE NOT EXISTS(
                        SELECT 1 FROM analysis_job
                        WHERE analysis_job.report_path = queue.report_path
                    ) AND NOT EXISTS(
                        SELECT 1 FROM analysis_cache
                        WHERE analysis_cache.report_path = queue.report_path
                    )
                    ORDER BY queue.report_path
                    """
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError(f"cannot purge retained state: {exc}") from exc
        return RetentionResult(
            deleted_jobs=max(job_cursor.rowcount, 0),
            deleted_cache_entries=max(cache_cursor.rowcount, 0),
            report_paths=tuple(row["report_path"] for row in queued_rows),
        )

    def confirm_report_deletions(self, report_paths: tuple[str, ...]) -> None:
        if not report_paths:
            return
        try:
            with self._transaction() as connection:
                connection.executemany(
                    "DELETE FROM report_cleanup_queue WHERE report_path = ?",
                    ((path,) for path in report_paths),
                )
        except sqlite3.Error as exc:
            raise StorageError("cannot confirm report cleanup") from exc

    def oldest_nonterminal_occurrence(self, source_name: str) -> datetime | None:
        terminal_values = tuple(status.value for status in TERMINAL_STATUSES)
        placeholders = ", ".join("?" for _ in terminal_values)
        with self._mutex:
            self._ensure_open()
            try:
                row = self._connection.execute(
                    f"""
                    SELECT MIN(occurred_at) FROM analysis_job
                    WHERE source_name = ? AND status NOT IN ({placeholders})
                      AND NOT (
                          status IN (?, ?) AND request_json IS NOT NULL
                      )
                    """,
                    (
                        source_name,
                        *terminal_values,
                        JobStatus.RETRY_WAIT.value,
                        JobStatus.ANALYZING.value,
                    ),
                ).fetchone()
            except sqlite3.Error as exc:
                raise StorageError("cannot read oldest nonterminal occurrence") from exc
        return _deserialize_time(row[0]) if row is not None and row[0] is not None else None

    def source_dependent_jobs(self, source_name: str) -> tuple[AnalysisJob, ...]:
        """Return nonterminal jobs that cannot resume without the source event."""

        with self._mutex:
            self._ensure_open()
            try:
                rows = self._connection.execute(
                    """
                    SELECT source_name, event_id, service, occurred_at, fingerprint,
                           git_commit, status, attempts, next_retry_at, last_error,
                           report_path, request_json, created_at, updated_at
                    FROM analysis_job
                    WHERE source_name = ? AND (
                        status IN (?, ?, ?)
                        OR (status = ? AND request_json IS NULL)
                    )
                    ORDER BY occurred_at, event_id
                    """,
                    (
                        source_name,
                        JobStatus.DISCOVERED.value,
                        JobStatus.PARSED.value,
                        JobStatus.SOURCE_RESOLVED.value,
                        JobStatus.ANALYZING.value,
                    ),
                ).fetchall()
            except sqlite3.Error as exc:
                raise StorageError("cannot read source-dependent jobs") from exc
        return tuple(self._job_from_row(row) for row in rows)

    def count_jobs(self, status: JobStatus | str | None = None) -> int:
        with self._mutex:
            self._ensure_open()
            try:
                if status is None:
                    row = self._connection.execute(
                        "SELECT COUNT(*) FROM analysis_job"
                    ).fetchone()
                else:
                    row = self._connection.execute(
                        "SELECT COUNT(*) FROM analysis_job WHERE status = ?",
                        (JobStatus(status).value,),
                    ).fetchone()
            except sqlite3.Error as exc:
                raise StorageError(f"cannot count jobs: {exc}") from exc
        return int(row[0])

    def count_occurrences(
        self,
        fingerprint: str,
        git_commit: str | None = None,
    ) -> int:
        with self._mutex:
            self._ensure_open()
            try:
                if git_commit is None:
                    row = self._connection.execute(
                        "SELECT COUNT(*) FROM analysis_job WHERE fingerprint = ?",
                        (fingerprint,),
                    ).fetchone()
                else:
                    row = self._connection.execute(
                        """
                        SELECT COUNT(*) FROM analysis_job
                        WHERE fingerprint = ? AND git_commit = ?
                        """,
                        (fingerprint, git_commit),
                    ).fetchone()
            except sqlite3.Error as exc:
                raise StorageError(f"cannot count occurrences: {exc}") from exc
        return int(row[0])

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> AnalysisJob:
        occurred_at = _deserialize_time(row["occurred_at"])
        created_at = _deserialize_time(row["created_at"])
        updated_at = _deserialize_time(row["updated_at"])
        assert occurred_at is not None and created_at is not None and updated_at is not None
        return AnalysisJob(
            source_name=row["source_name"],
            event_id=row["event_id"],
            service=row["service"],
            occurred_at=occurred_at,
            fingerprint=row["fingerprint"],
            git_commit=row["git_commit"],
            status=JobStatus(row["status"]),
            attempts=int(row["attempts"]),
            next_retry_at=_deserialize_time(row["next_retry_at"]),
            last_error=row["last_error"],
            report_path=row["report_path"],
            request_json=row["request_json"],
            created_at=created_at,
            updated_at=updated_at,
        )


AnalysisRepository = SQLiteStateStore
