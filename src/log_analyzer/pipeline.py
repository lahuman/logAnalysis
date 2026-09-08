"""One-shot orchestration for collecting and analyzing Java error events."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .analysis.models import AnalysisRequest, AnalysisResult, validated_evidence
from .analysis.openai_responses import (
    AnalyzerError,
    GlobalAnalyzerError,
    IncidentAnalyzer,
    InvalidResponseError,
    PermanentAnalyzerError,
    RetryableAnalyzerError,
)
from .analysis.redact import RedactionError, SecretRedactor
from .diagnostics import emit, error_summary
from .fingerprint import make_fingerprint
from .models import ErrorEvent, ErrorQuery, ParsedError, SourceContext
from .parsers import GenericErrorParser, JavaErrorParser, parse_event
from .report import ReportWriter
from .source_code.git import GitSourceResolver, ServiceSourceConfig, SourceResolutionError
from .sources.base import ErrorSource
from .storage import AnalysisJob, JobStatus, SQLiteStateStore, TERMINAL_STATUSES


Clock = Callable[[], datetime]


class PipelineInfrastructureError(RuntimeError):
    """An external dependency prevented a complete collection snapshot."""


@dataclass(slots=True)
class RunSummary:
    """Machine-readable outcome of one batch execution."""

    started_at: datetime
    snapshot_started_at: datetime
    snapshot_ended_at: datetime
    fetched_events: int = 0
    registered_events: int = 0
    duplicate_events: int = 0
    retry_jobs_processed: int = 0
    completed: int = 0
    cache_hits: int = 0
    no_source: int = 0
    retries_scheduled: int = 0
    permanent_failures: int = 0
    outstanding_retries: int = 0
    outstanding_permanent_failures: int = 0
    purged_jobs: int = 0
    purged_cache_entries: int = 0
    report_cleanup_failures: int = 0
    checkpoint_saved: bool = False
    interrupted: bool = False

    @property
    def has_failures(self) -> bool:
        return bool(
            self.interrupted
            or self.outstanding_retries
            or self.outstanding_permanent_failures
            or self.report_cleanup_failures
        )

    def to_dict(self) -> dict[str, Any]:
        values = asdict(self)
        for name in ("started_at", "snapshot_started_at", "snapshot_ended_at"):
            values[name] = _as_utc(values[name]).isoformat()
        values["has_failures"] = self.has_failures
        return values


class AnalysisPipeline:
    """Coordinate a bounded source snapshot without persisting paging cursors."""

    def __init__(
        self,
        *,
        source: ErrorSource,
        state: SQLiteStateStore,
        services: Mapping[str, ServiceSourceConfig],
        source_resolver: GitSourceResolver,
        analyzer: IncidentAnalyzer,
        report_writer: ReportWriter,
        source_name: str,
        model: str,
        prompt_version: str,
        analyzer_version: str,
        batch_size: int = 500,
        max_concurrency: int = 3,
        initial_lookback: timedelta = timedelta(minutes=60),
        ingestion_delay: timedelta = timedelta(seconds=60),
        overlap: timedelta = timedelta(minutes=5),
        severities: Sequence[str] = ("ERROR", "FATAL"),
        retention_days: int = 30,
        max_log_characters: int = 100_000,
        report_directory: str | Path | None = None,
        redactor: SecretRedactor | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not source_name.strip() or not model.strip():
            raise ValueError("source_name and model must not be empty")
        if not prompt_version.strip() or not analyzer_version.strip():
            raise ValueError("analysis identity versions must not be empty")
        if batch_size <= 0 or max_concurrency <= 0:
            raise ValueError("batch_size and max_concurrency must be positive")
        if initial_lookback <= timedelta(0):
            raise ValueError("initial_lookback must be positive")
        if ingestion_delay < timedelta(0) or overlap < timedelta(0):
            raise ValueError("ingestion_delay and overlap must not be negative")
        if retention_days <= 0 or max_log_characters <= 0:
            raise ValueError("retention_days and max_log_characters must be positive")

        self._source = source
        self._state = state
        self._services = dict(services)
        self._source_resolver = source_resolver
        self._analyzer = analyzer
        self._report_writer = report_writer
        self._source_name = source_name
        self._model = model
        self._prompt_version = prompt_version
        self._analyzer_version = analyzer_version
        self._batch_size = batch_size
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._initial_lookback = initial_lookback
        self._ingestion_delay = ingestion_delay
        self._overlap = overlap
        self._severities = tuple(severities)
        self._retention_days = retention_days
        self._max_log_characters = max_log_characters
        inferred_directory = getattr(report_writer, "_output_dir", None)
        directory = report_directory if report_directory is not None else inferred_directory
        self._report_directory = Path(directory) if directory is not None else None
        self._redactor = redactor or SecretRedactor()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._claimed_events: set[tuple[str, str]] = set()
        self._analysis_locks: dict[tuple[str, str, str, str, str], asyncio.Lock] = {}

        generic = GenericErrorParser()
        self._parsers = {
            name: (
                JavaErrorParser(
                    config.application_packages,
                    config.framework_packages,
                    max_input_chars=max_log_characters,
                ),
                generic,
            )
            for name, config in self._services.items()
        }
        self._fallback_parsers = (
            JavaErrorParser(max_input_chars=max_log_characters),
            generic,
        )

    async def healthcheck(self) -> None:
        """Check state, source, and every configured Git repository."""

        try:
            self._state.healthcheck()
        except Exception as exc:
            raise PipelineInfrastructureError("state database healthcheck failed") from exc
        try:
            await self._source.healthcheck()
        except Exception as exc:
            raise PipelineInfrastructureError("error source healthcheck failed") from exc
        try:
            await asyncio.to_thread(self._source_resolver.healthcheck)
        except Exception as exc:
            raise PipelineInfrastructureError("Git repository healthcheck failed") from exc

    async def run(self, *, stop_event: asyncio.Event | None = None) -> RunSummary:
        """Drain due retries, process one fixed source snapshot, then checkpoint it."""

        self._claimed_events.clear()
        self._analysis_locks.clear()
        stop = stop_event or asyncio.Event()
        started_at = _as_utc(self._clock())
        ended_at = started_at - self._ingestion_delay
        checkpoint = self._state.get_checkpoint(self._source_name)
        if checkpoint is None:
            query_started_at = ended_at - self._initial_lookback
        else:
            query_started_at = checkpoint.ended_at - self._overlap
        oldest_nonterminal = self._state.oldest_nonterminal_occurrence(
            self._source_name
        )
        if oldest_nonterminal is not None:
            query_started_at = min(
                query_started_at,
                oldest_nonterminal - self._overlap,
            )
        if query_started_at >= ended_at:
            query_started_at = ended_at - timedelta(microseconds=1)

        summary = RunSummary(
            started_at=started_at,
            snapshot_started_at=query_started_at,
            snapshot_ended_at=ended_at,
        )

        self._state.recover_interrupted_analysis(now=started_at)
        await self._drain_due_retries(summary, stop, started_at)
        if stop.is_set():
            summary.interrupted = True
            self._set_outstanding_counts(summary)
            return summary

        query = ErrorQuery(
            started_at=query_started_at,
            ended_at=ended_at,
            severities=self._severities,
            limit=self._batch_size,
        )
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_event_keys: set[tuple[str, str]] = set()
        while True:
            if stop.is_set():
                summary.interrupted = True
                break
            try:
                page = await self._source.fetch(query, cursor)
            except Exception as exc:
                raise PipelineInfrastructureError("error source fetch failed") from exc

            summary.fetched_events += len(page.events)
            seen_event_keys.update(
                (event.source_name, event.event_id) for event in page.events
            )
            results, next_cursor = await self._process_page(
                page.events,
                page.next_cursor,
                summary,
                stop,
                started_at,
            )
            for result in results:
                if isinstance(result, BaseException):
                    if isinstance(result, PipelineInfrastructureError):
                        raise result
                    raise PipelineInfrastructureError(
                        "event processing infrastructure failed"
                    ) from result
                if result is False:
                    summary.interrupted = True

            if summary.interrupted:
                break
            if next_cursor is None:
                self._fail_unrecoverable_jobs(
                    query,
                    seen_event_keys,
                    summary,
                    started_at,
                )
                self._state.save_checkpoint(
                    self._source_name,
                    None,
                    ended_at,
                    now=started_at,
                )
                summary.checkpoint_saved = True
                break
            if next_cursor == cursor or next_cursor in seen_cursors:
                raise PipelineInfrastructureError(
                    "error source returned a repeated page cursor"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        if summary.checkpoint_saved:
            await self._apply_retention(summary, started_at)
        self._set_outstanding_counts(summary)
        return summary

    def _fail_unrecoverable_jobs(
        self,
        query: ErrorQuery,
        seen_event_keys: set[tuple[str, str]],
        summary: RunSummary,
        now: datetime,
    ) -> None:
        for job in self._state.source_dependent_jobs(self._source_name):
            key = (job.source_name, job.event_id)
            if key in seen_event_keys:
                continue
            if not query.started_at <= job.occurred_at <= query.ended_at:
                continue
            updated = self._state.set_status(
                job.source_name,
                job.event_id,
                JobStatus.PERMANENT_FAILURE,
                error="Source event unavailable while recovering interrupted work",
                now=now,
            )
            if updated:
                summary.permanent_failures += 1
                emit(
                    "error", "analysis_permanent_failure", source_name=job.source_name,
                    event_id=job.event_id, service=job.service,
                    reason="Source event unavailable while recovering interrupted work",
                )

    async def _process_page(
        self,
        events: Sequence[ErrorEvent],
        next_cursor: str | None,
        summary: RunSummary,
        stop: asyncio.Event,
        now: datetime,
    ) -> tuple[list[bool | BaseException], str | None]:
        pending = asyncio.gather(
            *(self._process_event_guarded(event, summary, stop, now) for event in events),
            return_exceptions=True,
        )
        refresh = getattr(self._source, "refresh_cursor", None)
        interval = getattr(self._source, "cursor_refresh_interval_seconds", None)
        if next_cursor is None or not callable(refresh) or not isinstance(interval, (int, float)):
            return list(await pending), next_cursor

        try:
            while True:
                done, _ = await asyncio.wait((pending,), timeout=float(interval))
                if done:
                    return list(await pending), next_cursor
                next_cursor = await refresh(next_cursor)
        except Exception as exc:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            raise PipelineInfrastructureError("error source cursor refresh failed") from exc

    async def _drain_due_retries(
        self,
        summary: RunSummary,
        stop: asyncio.Event,
        now: datetime,
    ) -> None:
        while not stop.is_set():
            jobs = self._state.due_retries(now=now, limit=self._batch_size)
            if not jobs:
                return
            results = await asyncio.gather(
                *(self._process_retry_guarded(job, summary, stop, now) for job in jobs),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    if isinstance(result, PipelineInfrastructureError):
                        raise result
                    raise PipelineInfrastructureError(
                        "retry processing infrastructure failed"
                    ) from result
            if any(result is False for result in results):
                return

    async def _process_event_guarded(
        self,
        event: ErrorEvent,
        summary: RunSummary,
        stop: asyncio.Event,
        now: datetime,
    ) -> bool:
        async with self._semaphore:
            if stop.is_set():
                return False
            try:
                await self._process_event(event, summary, now)
            except Exception as exc:
                emit(
                    "error", "event_processing_failed", error=exc, redactor=self._redactor,
                    source_name=event.source_name, event_id=event.event_id, service=event.service,
                )
                if isinstance(exc, PipelineInfrastructureError):
                    stop.set()
                raise
            return True

    async def _process_retry_guarded(
        self,
        job: AnalysisJob,
        summary: RunSummary,
        stop: asyncio.Event,
        now: datetime,
    ) -> bool:
        async with self._semaphore:
            if stop.is_set():
                return False
            summary.retry_jobs_processed += 1
            try:
                await self._process_retry(job, summary, now)
            except Exception as exc:
                emit(
                    "error", "retry_processing_failed", error=exc, redactor=self._redactor,
                    source_name=job.source_name, event_id=job.event_id, service=job.service,
                )
                if isinstance(exc, PipelineInfrastructureError):
                    stop.set()
                raise
            return True

    async def _process_event(
        self, event: ErrorEvent, summary: RunSummary, now: datetime
    ) -> None:
        parsers = self._parsers.get(event.service, self._fallback_parsers)
        try:
            parsed = parse_event(event, parsers)
            fingerprint = make_fingerprint(event, parsed)
        except Exception as exc:
            raise PipelineInfrastructureError("error parsing failed unexpectedly") from exc

        inserted = self._state.register_event(event, fingerprint, now=now)
        event_key = (event.source_name, event.event_id)
        resolved_commit_hint: str | None = None
        if inserted:
            self._claimed_events.add(event_key)
            summary.registered_events += 1
        else:
            summary.duplicate_events += 1
            if event_key in self._claimed_events:
                return
            existing = self._state.get_job(event.source_name, event.event_id)
            if existing is None:
                raise PipelineInfrastructureError(
                    "registered event could not be reloaded"
                )
            if existing.status in TERMINAL_STATUSES or existing.status is JobStatus.RETRY_WAIT:
                return
            fingerprint = existing.fingerprint
            if not event.git_commit:
                resolved_commit_hint = existing.git_commit

        if event.attributes.get("mapping_error") is True:
            self._mark_permanent(
                event.source_name,
                event.event_id,
                ValueError("error source document could not be mapped"),
                summary,
                now,
            )
            return

        self._require_status(event, JobStatus.PARSED, now=now)
        reason: str | None = None
        if event.service not in self._services:
            reason = "No configured source repository exists for this service."

        source_context: SourceContext | None = None
        if reason is None:
            try:
                source_context = await asyncio.to_thread(
                    self._source_resolver.resolve,
                    event,
                    parsed,
                    resolved_commit_hint=resolved_commit_hint,
                )
            except SourceResolutionError as exc:
                raise PipelineInfrastructureError("Git source resolution failed") from exc
            if source_context is None:
                reason = (
                    "The Java stack frame could not be resolved against the "
                    "configured Git source revision."
                )

        if source_context is None:
            report_path = await self._write_no_source(
                event,
                parsed,
                fingerprint,
                reason or "Source unavailable.",
                now,
            )
            self._require_status(
                event,
                JobStatus.NO_SOURCE,
                report_path=report_path,
                now=now,
            )
            summary.no_source += 1
            return

        if not self._state.set_resolved_git_commit(
            event.source_name,
            event.event_id,
            source_context.git_commit,
            now=now,
        ):
            raise PipelineInfrastructureError(
                "analysis job source revision changed unexpectedly"
            )
        self._require_status(event, JobStatus.SOURCE_RESOLVED, now=now)
        try:
            request = self._build_request(event, parsed, source_context, fingerprint)
            safe_request = self._redactor.redact_request(request)
        except (ValidationError, ValueError, RedactionError) as exc:
            self._mark_permanent(
                event.source_name, event.event_id, exc, summary, now
            )
            return

        await self._analyze(
            source_name=event.source_name,
            event_id=event.event_id,
            occurred_at=event.occurred_at,
            fingerprint=fingerprint,
            git_commit=source_context.git_commit,
            request=safe_request,
            summary=summary,
            now=now,
            is_retry=False,
        )

    async def _process_retry(
        self, job: AnalysisJob, summary: RunSummary, now: datetime
    ) -> None:
        if not job.request_json:
            self._mark_permanent(
                job.source_name,
                job.event_id,
                ValueError("redacted retry context is unavailable"),
                summary,
                now,
            )
            return
        try:
            request = AnalysisRequest.model_validate_json(job.request_json)
            request = self._redactor.redact_request(request)
        except (ValidationError, ValueError, RedactionError) as exc:
            self._mark_permanent(
                job.source_name, job.event_id, exc, summary, now
            )
            return
        if (
            request.service != job.service
            or request.fingerprint != job.fingerprint
            or request.git_commit != job.git_commit
        ):
            self._mark_permanent(
                job.source_name,
                job.event_id,
                ValueError("retry context identity does not match its job"),
                summary,
                now,
            )
            return

        await self._analyze(
            source_name=job.source_name,
            event_id=job.event_id,
            occurred_at=job.occurred_at,
            fingerprint=job.fingerprint,
            git_commit=request.git_commit,
            request=request,
            summary=summary,
            now=now,
            is_retry=True,
        )

    async def _analyze(
        self,
        *,
        source_name: str,
        event_id: str,
        occurred_at: datetime,
        fingerprint: str,
        git_commit: str,
        request: AnalysisRequest,
        summary: RunSummary,
        now: datetime,
        is_retry: bool,
    ) -> None:
        identity = (
            fingerprint,
            git_commit,
            self._model,
            self._prompt_version,
            self._analyzer_version,
        )
        lock = self._analysis_locks.setdefault(identity, asyncio.Lock())
        async with lock:
            await self._analyze_locked(
                source_name=source_name,
                event_id=event_id,
                occurred_at=occurred_at,
                fingerprint=fingerprint,
                git_commit=git_commit,
                request=request,
                summary=summary,
                now=now,
                is_retry=is_retry,
            )

    async def _analyze_locked(
        self,
        *,
        source_name: str,
        event_id: str,
        occurred_at: datetime,
        fingerprint: str,
        git_commit: str,
        request: AnalysisRequest,
        summary: RunSummary,
        now: datetime,
        is_retry: bool,
    ) -> None:
        request_json = request.to_prompt_json()
        cache = self._state.get_analysis_cache(
            fingerprint=fingerprint,
            git_commit=git_commit,
            model=self._model,
            prompt_version=self._prompt_version,
            analyzer_version=self._analyzer_version,
        )
        result: AnalysisResult
        if cache is not None:
            try:
                result = AnalysisResult.model_validate_json(cache.analysis_json)
                result = self._redactor.redact_result(result)
                result = validated_evidence(result, request)
            except (ValidationError, ValueError, RedactionError) as exc:
                raise PipelineInfrastructureError(
                    "stored analysis cache is invalid"
                ) from exc
            summary.cache_hits += 1
        else:
            # A due retry remains RETRY_WAIT while the remote call is in flight.
            # A crash therefore leaves it due instead of stranded in ANALYZING.
            if not is_retry:
                updated = self._state.set_status(
                    source_name,
                    event_id,
                    JobStatus.ANALYZING,
                    request_json=request_json,
                    now=now,
                )
                if not updated:
                    raise PipelineInfrastructureError("analysis job disappeared")
            try:
                analyzed = await self._analyzer.analyze(request)
                result = AnalysisResult.model_validate(analyzed)
                result = self._redactor.redact_result(result)
                result = validated_evidence(result, request)
            except GlobalAnalyzerError as exc:
                raise PipelineInfrastructureError(
                    "LLM request contract or authorization failed"
                ) from exc
            except RetryableAnalyzerError as exc:
                status = self._state.mark_retry(
                    source_name,
                    event_id,
                    error_summary(exc, self._redactor),
                    request_json=request_json,
                    now=now,
                    max_attempts=5,
                )
                if status is JobStatus.RETRY_WAIT:
                    summary.retries_scheduled += 1
                else:
                    summary.permanent_failures += 1
                emit(
                    "warning" if status is JobStatus.RETRY_WAIT else "error",
                    "analysis_retry_scheduled" if status is JobStatus.RETRY_WAIT else "analysis_permanent_failure",
                    error=exc, redactor=self._redactor, source_name=source_name,
                    event_id=event_id, service=request.service, status=status.value,
                )
                return
            except (
                PermanentAnalyzerError,
                InvalidResponseError,
                AnalyzerError,
                ValidationError,
                RedactionError,
            ) as exc:
                self._mark_permanent(
                    source_name, event_id, exc, summary, now
                )
                return

        occurrence_count = self._state.count_occurrences(fingerprint, git_commit)
        try:
            report_path = await asyncio.to_thread(
                self._report_writer.write,
                request,
                result,
                event_id=event_id,
                source_name=source_name,
                occurrence_count=max(occurrence_count, 1),
                first_seen=occurred_at,
                last_seen=occurred_at,
                model=self._model,
                prompt_version=self._prompt_version,
                analyzed_at=now,
            )
        except OSError as exc:
            raise PipelineInfrastructureError(
                "analysis report could not be written"
            ) from exc

        analysis_json = result.model_dump_json()
        self._state.save_analysis_cache(
            fingerprint=fingerprint,
            git_commit=git_commit,
            model=self._model,
            prompt_version=self._prompt_version,
            analyzer_version=self._analyzer_version,
            analysis_json=analysis_json,
            report_path=report_path,
            now=now,
        )
        updated = self._state.set_status(
            source_name,
            event_id,
            JobStatus.COMPLETED,
            report_path=report_path,
            now=now,
        )
        if not updated:
            raise PipelineInfrastructureError("completed analysis job disappeared")
        summary.completed += 1

    async def _write_no_source(
        self,
        event: ErrorEvent,
        parsed: ParsedError,
        fingerprint: str,
        reason: str,
        now: datetime,
    ) -> Path:
        count = self._state.count_occurrences(fingerprint, event.git_commit)
        try:
            return await asyncio.to_thread(
                self._report_writer.write_no_source,
                event_id=event.event_id,
                source_name=event.source_name,
                service=event.service,
                environment=event.environment or "unknown",
                version=event.version or "unknown",
                git_commit=event.git_commit or "unknown",
                error_type=parsed.error_type or event.error_type or "UnknownError",
                message=parsed.message or event.message or "No error message",
                stack_trace=_event_text(event),
                reason=reason,
                occurrence_count=max(count, 1),
                first_seen=event.occurred_at,
                last_seen=event.occurred_at,
                analyzed_at=now,
            )
        except OSError as exc:
            raise PipelineInfrastructureError(
                "NO_SOURCE report could not be written"
            ) from exc

    def _build_request(
        self,
        event: ErrorEvent,
        parsed: ParsedError,
        context: SourceContext,
        fingerprint: str,
    ) -> AnalysisRequest:
        return AnalysisRequest(
            service=_bounded(event.service, 200),
            environment=_bounded(event.environment or "unknown", 100),
            version=_bounded(event.version or "unknown", 200),
            fingerprint=fingerprint,
            error_type=_bounded(
                parsed.error_type or event.error_type or "UnknownError", 500
            ),
            message=_bounded(
                parsed.message or event.message or "No error message", 8_000
            ),
            stack_trace=_bounded(_event_text(event), 30_000),
            parse_warnings=tuple(
                _bounded(item, 2_000) for item in parsed.parse_warnings[:50]
            ),
            git_commit=_bounded(context.git_commit, 128),
            source_path=context.source_path,
            line_number=context.line_number,
            function_name=(
                _bounded(context.function_name, 1_000)
                if context.function_name is not None
                else None
            ),
            class_name=_bounded(context.class_name, 1_000),
            source_code=_bounded(context.source_code, 100_000),
            context_start_line=context.context_start_line,
            context_end_line=context.context_end_line,
            revision_source=context.revision_source,
            git_reference=(
                _bounded(context.git_reference, 512)
                if context.git_reference is not None
                else None
            ),
            git_change_context=_bounded(context.git_change_context, 30_000),
        )

    def _require_status(
        self,
        event: ErrorEvent,
        status: JobStatus,
        *,
        report_path: str | Path | None = None,
        now: datetime,
    ) -> None:
        if not self._state.set_status(
            event.source_name,
            event.event_id,
            status,
            report_path=report_path,
            now=now,
        ):
            raise PipelineInfrastructureError(
                "analysis job disappeared during status update"
            )

    def _mark_permanent(
        self,
        source_name: str,
        event_id: str,
        error: BaseException,
        summary: RunSummary,
        now: datetime,
    ) -> None:
        if not self._state.set_status(
            source_name,
            event_id,
            JobStatus.PERMANENT_FAILURE,
            error=error_summary(error, self._redactor),
            now=now,
        ):
            raise PipelineInfrastructureError("failed analysis job disappeared")
        summary.permanent_failures += 1
        emit(
            "error", "analysis_permanent_failure", error=error, redactor=self._redactor,
            source_name=source_name, event_id=event_id, status=JobStatus.PERMANENT_FAILURE.value,
        )

    async def _apply_retention(self, summary: RunSummary, now: datetime) -> None:
        retained = self._state.purge_older_than(
            now - timedelta(days=self._retention_days)
        )
        summary.purged_jobs = retained.deleted_jobs
        summary.purged_cache_entries = retained.deleted_cache_entries
        if not retained.report_paths or self._report_directory is None:
            return
        failed, deleted = await asyncio.to_thread(
            _delete_reports_safely,
            retained.report_paths,
            self._report_directory,
        )
        self._state.confirm_report_deletions(deleted)
        summary.report_cleanup_failures = len(failed)
        if failed:
            emit("warning", "report_cleanup_failed", paths=failed, redactor=self._redactor)

    def _set_outstanding_counts(self, summary: RunSummary) -> None:
        summary.outstanding_retries = self._state.count_jobs(JobStatus.RETRY_WAIT)
        summary.outstanding_permanent_failures = self._state.count_jobs(
            JobStatus.PERMANENT_FAILURE
        )


Pipeline = AnalysisPipeline


def _event_text(event: ErrorEvent) -> str:
    return event.stack_trace or event.message or ""


def _bounded(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    marker = "\n[TRUNCATED]"
    return value[: maximum - len(marker)] + marker


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _delete_reports_safely(
    paths: Sequence[str], report_directory: Path
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        root = report_directory.resolve(strict=False)
    except OSError:
        return tuple(paths), ()
    failed: list[str] = []
    deleted: list[str] = []
    for value in paths:
        try:
            candidate = Path(value).resolve(strict=False)
            if not candidate.is_relative_to(root):
                failed.append(value)
                continue
            candidate.unlink(missing_ok=True)
            deleted.append(value)
        except OSError:
            failed.append(value)
    return tuple(failed), tuple(deleted)
