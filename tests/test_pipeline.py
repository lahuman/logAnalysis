from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from log_analyzer.analysis.models import AnalysisRequest, AnalysisResult
from log_analyzer.analysis.openai_responses import (
    GlobalAnalyzerError,
    InvalidResponseError,
    RetryableAnalyzerError,
)
from log_analyzer.models import ErrorEvent, ErrorPage, SourceContext
from log_analyzer.pipeline import AnalysisPipeline, PipelineInfrastructureError
from log_analyzer.storage import JobStatus, SQLiteStateStore


NOW = datetime(2026, 9, 4, 3, 0, tzinfo=UTC)
COMMIT = "a" * 40
STACK = """java.lang.IllegalStateException: bad order
	at com.example.order.OrderService.submit(OrderService.java:42)
	at org.springframework.web.DispatcherServlet.doDispatch(DispatcherServlet.java:1)"""


def event(event_id: str = "event-1", **changes: object) -> ErrorEvent:
    values: dict[str, object] = {
        "source_name": "logs",
        "event_id": event_id,
        "occurred_at": NOW - timedelta(minutes=2),
        "service": "orders",
        "severity": "ERROR",
        "message": "bad order",
        "environment": "production",
        "version": "1.2.3",
        "git_commit": COMMIT,
        "error_type": "java.lang.IllegalStateException",
        "stack_trace": STACK,
        "language_hint": "java",
    }
    values.update(changes)
    return ErrorEvent(**values)


def context(**changes: object) -> SourceContext:
    values: dict[str, object] = {
        "service": "orders",
        "git_commit": COMMIT,
        "repository_path": Path("/repository"),
        "source_path": "src/main/java/com/example/order/OrderService.java",
        "line_number": 42,
        "function_name": "submit",
        "class_name": "com.example.order.OrderService",
        "source_code": "\n".join(f"line {line}" for line in range(20, 61)),
        "context_start_line": 20,
        "context_end_line": 60,
    }
    values.update(changes)
    return SourceContext(**values)  # type: ignore[arg-type]


def result() -> AnalysisResult:
    return AnalysisResult(
        summary="The supplied source can reject the order.",
        root_causes=[],
        recommended_fixes=[],
        validation_steps=["Run the order service regression test."],
        unknowns=[],
    )


class PagedSource:
    def __init__(self, pages: dict[str | None, ErrorPage | BaseException]) -> None:
        self.pages = pages
        self.calls: list[tuple[object, str | None]] = []
        self.closed = False

    async def fetch(self, query, cursor=None):
        self.calls.append((query, cursor))
        value = self.pages[cursor]
        if isinstance(value, BaseException):
            raise value
        return value

    async def healthcheck(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class Resolver:
    def __init__(self, value: SourceContext | None = None) -> None:
        self.value = value
        self.calls = 0

    def resolve(self, event, parsed, *, resolved_commit_hint=None):
        self.calls += 1
        return self.value

    def healthcheck(self) -> None:
        return None


class Analyzer:
    def __init__(self, outcomes: list[AnalysisResult | BaseException] | None = None) -> None:
        self.outcomes = list(outcomes or [result()])
        self.requests: list[AnalysisRequest] = []

    async def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if self.outcomes else result()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class Writer:
    def __init__(self, directory: Path, timeline: list[str] | None = None) -> None:
        self._output_dir = directory
        self.timeline = timeline
        self.completed: list[str] = []
        self.no_source: list[str] = []

    def write(self, request, analysis, *, event_id, **kwargs):
        if self.timeline is not None:
            self.timeline.append(f"write:{event_id}")
        self.completed.append(event_id)
        return self._output_dir / f"{event_id}.md"

    def write_no_source(self, *, event_id, **kwargs):
        self.no_source.append(event_id)
        return self._output_dir / f"{event_id}.md"


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.store = SQLiteStateStore(self.directory / "state.db")
        self.service = SimpleNamespace(
            repository=Path("/repository"),
            source_roots=(PurePosixPath("src/main/java"),),
            application_packages=("com.example.order",),
            framework_packages=("org.springframework",),
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def pipeline(
        self,
        source,
        *,
        resolver: Resolver | None = None,
        analyzer: Analyzer | None = None,
        writer: Writer | None = None,
        clock=None,
        batch_size: int = 2,
    ) -> AnalysisPipeline:
        return AnalysisPipeline(
            source=source,
            state=self.store,
            services={"orders": self.service},
            source_resolver=resolver or Resolver(context()),
            analyzer=analyzer or Analyzer(),
            report_writer=writer or Writer(self.directory),
            source_name="logs",
            model="test-model",
            prompt_version="prompt-1",
            analyzer_version="analyzer-1",
            batch_size=batch_size,
            max_concurrency=2,
            initial_lookback=timedelta(minutes=60),
            ingestion_delay=timedelta(0),
            overlap=timedelta(minutes=5),
            report_directory=self.directory,
            clock=clock or (lambda: NOW),
        )

    async def test_pages_fixed_snapshot_and_saves_only_final_checkpoint(self) -> None:
        first = event("first")
        second = event("second")
        source = PagedSource(
            {
                None: ErrorPage((first,), "opaque-pit-cursor"),
                "opaque-pit-cursor": ErrorPage((second,), None),
            }
        )
        analyzer = Analyzer()
        writer = Writer(self.directory)
        pipeline = self.pipeline(source, analyzer=analyzer, writer=writer)

        summary = await pipeline.run()

        self.assertTrue(summary.checkpoint_saved)
        self.assertEqual(summary.fetched_events, 2)
        self.assertEqual(summary.completed, 2)
        self.assertEqual(summary.cache_hits, 1)
        self.assertEqual(len(analyzer.requests), 1)
        self.assertEqual([cursor for _, cursor in source.calls], [None, "opaque-pit-cursor"])
        queries = [query for query, _ in source.calls]
        self.assertEqual({query.ended_at for query in queries}, {NOW})
        self.assertEqual(queries[0].started_at, NOW - timedelta(minutes=60))
        checkpoint = self.store.get_checkpoint("logs")
        self.assertIsNotNone(checkpoint)
        self.assertIsNone(checkpoint.cursor)
        self.assertEqual(checkpoint.ended_at, NOW)

        duplicate = await pipeline.run()
        self.assertEqual(duplicate.duplicate_events, 2)
        self.assertEqual(duplicate.completed, 0)
        self.assertEqual(len(analyzer.requests), 1)

    async def test_refreshes_source_cursor_while_page_processing_is_slow(self) -> None:
        class RefreshingSource(PagedSource):
            cursor_refresh_interval_seconds = 0.001

            def __init__(inner_self):
                super().__init__(
                    {
                        None: ErrorPage((event(),), "next"),
                        "next": ErrorPage((), None),
                    }
                )
                inner_self.refreshes = 0

            async def refresh_cursor(inner_self, cursor):
                inner_self.refreshes += 1
                return cursor

        class SlowAnalyzer(Analyzer):
            async def analyze(inner_self, request):
                await asyncio.sleep(0.01)
                return await super().analyze(request)

        source = RefreshingSource()
        await self.pipeline(source, analyzer=SlowAnalyzer()).run()

        self.assertGreater(source.refreshes, 0)
        self.assertEqual([None, "next"], [cursor for _, cursor in source.calls])

    async def test_concurrent_same_fingerprint_uses_one_llm_call(self) -> None:
        class SlowAnalyzer(Analyzer):
            async def analyze(inner_self, request):
                inner_self.requests.append(request)
                await asyncio.sleep(0.05)
                return result()

        source = PagedSource(
            {None: ErrorPage((event("first"), event("second")), None)}
        )
        analyzer = SlowAnalyzer()

        summary = await self.pipeline(source, analyzer=analyzer).run()

        self.assertEqual(summary.completed, 2)
        self.assertEqual(summary.cache_hits, 1)
        self.assertEqual(len(analyzer.requests), 1)

    async def test_missing_commit_uses_repository_revision_and_calls_analyzer(self) -> None:
        missing_commit = event(git_commit=None)
        source = PagedSource({None: ErrorPage((missing_commit,), None)})
        resolver = Resolver(
            context(
                revision_source="repository_ref",
                git_reference="main",
                git_change_context="commit abc\n-null guard\n+direct dereference",
            )
        )
        analyzer = Analyzer()
        writer = Writer(self.directory)

        summary = await self.pipeline(
            source,
            resolver=resolver,
            analyzer=analyzer,
            writer=writer,
        ).run()

        self.assertEqual(summary.completed, 1)
        self.assertEqual(summary.no_source, 0)
        self.assertEqual(resolver.calls, 1)
        self.assertEqual(len(analyzer.requests), 1)
        self.assertEqual(analyzer.requests[0].revision_source, "repository_ref")
        self.assertEqual(analyzer.requests[0].git_reference, "main")
        self.assertIn("direct dereference", analyzer.requests[0].git_change_context)
        self.assertEqual(writer.no_source, [])
        job = self.store.get_job("logs", "event-1")
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(job.git_commit, COMMIT)
        self.assertIsNotNone(job.report_path)

    async def test_unmappable_source_document_is_permanent_without_llm_call(self) -> None:
        invalid = event(
            service="__unmapped__",
            git_commit=None,
            attributes={"mapping_error": True},
        )
        source = PagedSource({None: ErrorPage((invalid,), None)})
        analyzer = Analyzer()

        summary = await self.pipeline(source, analyzer=analyzer).run()

        self.assertEqual(summary.permanent_failures, 1)
        self.assertTrue(summary.has_failures)
        self.assertEqual(analyzer.requests, [])
        self.assertEqual(
            self.store.get_job("logs", "event-1").status,
            JobStatus.PERMANENT_FAILURE,
        )

    async def test_retry_context_is_redacted_and_due_retry_runs_before_fetch(self) -> None:
        secret_event = event(
            message="password=super-secret",
            stack_trace=STACK + "\npassword=super-secret",
        )
        timeline: list[str] = []

        class OrderedSource(PagedSource):
            async def fetch(inner_self, query, cursor=None):
                timeline.append("fetch")
                return await super().fetch(query, cursor)

        class OrderedAnalyzer(Analyzer):
            async def analyze(inner_self, request):
                timeline.append("analyze")
                return await super().analyze(request)

        source = OrderedSource({None: ErrorPage((secret_event,), None)})
        analyzer = OrderedAnalyzer(
            [RetryableAnalyzerError("temporary"), result()]
        )
        writer = Writer(self.directory, timeline)
        current = [NOW]
        pipeline = self.pipeline(
            source,
            analyzer=analyzer,
            writer=writer,
            clock=lambda: current[0],
        )

        first = await pipeline.run()

        self.assertEqual(first.retries_scheduled, 1)
        retry_job = self.store.get_job("logs", "event-1")
        self.assertEqual(retry_job.status, JobStatus.RETRY_WAIT)
        self.assertIsNotNone(retry_job.request_json)
        self.assertNotIn("super-secret", retry_job.request_json)
        self.assertIn("[REDACTED_SECRET]", retry_job.request_json)

        timeline.clear()
        current[0] = NOW + timedelta(seconds=61)
        second = await pipeline.run()

        self.assertEqual(second.retry_jobs_processed, 1)
        self.assertEqual(timeline[0], "analyze")
        self.assertIn("fetch", timeline)
        completed = self.store.get_job("logs", "event-1")
        self.assertEqual(completed.status, JobStatus.COMPLETED)
        self.assertIsNone(completed.request_json)

    async def test_due_retry_queue_is_drained_across_batches_before_fetch(self) -> None:
        for index in range(3):
            retry_event = event(f"retry-{index}")
            fingerprint = f"fingerprint-{index}"
            self.store.register_event(
                retry_event,
                fingerprint,
                now=NOW - timedelta(minutes=3),
            )
            request = AnalysisRequest(
                service="orders",
                environment="production",
                version="1.2.3",
                fingerprint=fingerprint,
                error_type="java.lang.IllegalStateException",
                message="bad order",
                stack_trace=STACK,
                git_commit=COMMIT,
                source_path="src/main/java/com/example/order/OrderService.java",
                line_number=42,
                function_name="submit",
                class_name="com.example.order.OrderService",
                source_code="line 42",
                context_start_line=42,
                context_end_line=42,
            )
            self.store.mark_retry(
                "logs",
                retry_event.event_id,
                "temporary",
                request_json=request.to_prompt_json(),
                now=NOW - timedelta(minutes=2),
            )

        timeline: list[str] = []

        class OrderedSource(PagedSource):
            async def fetch(inner_self, query, cursor=None):
                timeline.append("fetch")
                return await super().fetch(query, cursor)

        class OrderedAnalyzer(Analyzer):
            async def analyze(inner_self, request):
                timeline.append("analyze")
                return await super().analyze(request)

        source = OrderedSource({None: ErrorPage((), None)})
        analyzer = OrderedAnalyzer([result(), result(), result()])

        summary = await self.pipeline(
            source,
            analyzer=analyzer,
            batch_size=2,
        ).run()

        self.assertEqual(summary.retry_jobs_processed, 3)
        self.assertEqual(summary.completed, 3)
        self.assertEqual(len(analyzer.requests), 3)
        self.assertEqual(timeline[-1], "fetch")

    async def test_invalid_analyzer_response_is_permanent_but_checkpoint_advances(self) -> None:
        source = PagedSource({None: ErrorPage((event(),), None)})
        analyzer = Analyzer([InvalidResponseError("invalid structured output")])
        pipeline = self.pipeline(source, analyzer=analyzer)

        summary = await pipeline.run()

        self.assertTrue(summary.checkpoint_saved)
        self.assertTrue(summary.has_failures)
        self.assertEqual(summary.permanent_failures, 1)
        self.assertEqual(
            self.store.get_job("logs", "event-1").status,
            JobStatus.PERMANENT_FAILURE,
        )

        later = await pipeline.run()
        self.assertEqual(later.outstanding_permanent_failures, 1)
        self.assertEqual(later.permanent_failures, 0)
        self.assertTrue(later.has_failures)

    async def test_global_openai_error_stops_snapshot_without_checkpoint(self) -> None:
        source = PagedSource(
            {None: ErrorPage(tuple(event(f"event-{i}") for i in range(10)), None)}
        )

        class GlobalFailureAnalyzer(Analyzer):
            async def analyze(inner_self, request):
                inner_self.requests.append(request)
                raise GlobalAnalyzerError("bad model")

        analyzer = GlobalFailureAnalyzer()

        with self.assertRaises(PipelineInfrastructureError):
            await self.pipeline(source, analyzer=analyzer).run()

        self.assertIsNone(self.store.get_checkpoint("logs"))
        self.assertLessEqual(len(analyzer.requests), 2)
        self.assertEqual(
            self.store.get_job("logs", "event-0").status,
            JobStatus.ANALYZING,
        )

    async def test_model_output_is_redacted_before_cache_persistence(self) -> None:
        secret_result = result().model_copy(
            update={"summary": "password=model-secret"}
        )
        source = PagedSource({None: ErrorPage((event(),), None)})

        await self.pipeline(source, analyzer=Analyzer([secret_result])).run()

        job = self.store.get_job("logs", "event-1")
        cached = self.store.get_analysis_cache(
            fingerprint=job.fingerprint,
            git_commit=COMMIT,
            model="test-model",
            prompt_version="prompt-1",
            analyzer_version="analyzer-1",
        )
        self.assertIsNotNone(cached)
        self.assertNotIn("model-secret", cached.analysis_json)
        self.assertIn("[REDACTED_SECRET]", cached.analysis_json)

    async def test_nonterminal_job_extends_next_query_window(self) -> None:
        old_event = event(occurred_at=NOW - timedelta(hours=3))
        self.store.register_event(old_event, "old-fingerprint", now=NOW)
        self.store.set_status("logs", "event-1", JobStatus.PARSED, now=NOW)
        source = PagedSource({None: ErrorPage((), None)})

        await self.pipeline(source).run()

        query, _ = source.calls[0]
        self.assertLessEqual(
            query.started_at,
            old_event.occurred_at - timedelta(minutes=5),
        )
        self.assertEqual(
            self.store.get_job("logs", "event-1").status,
            JobStatus.PERMANENT_FAILURE,
        )

    async def test_source_failure_never_advances_checkpoint(self) -> None:
        source = PagedSource(
            {
                None: ErrorPage((event(),), "next"),
                "next": RuntimeError("source unavailable"),
            }
        )

        with self.assertRaises(PipelineInfrastructureError):
            await self.pipeline(source).run()

        self.assertIsNone(self.store.get_checkpoint("logs"))

    async def test_pre_set_stop_event_does_not_touch_source_or_checkpoint(self) -> None:
        source = PagedSource({None: ErrorPage((), None)})
        stop = __import__("asyncio").Event()
        stop.set()

        summary = await self.pipeline(source).run(stop_event=stop)

        self.assertTrue(summary.interrupted)
        self.assertFalse(summary.checkpoint_saved)
        self.assertEqual(source.calls, [])
        self.assertIsNone(self.store.get_checkpoint("logs"))


if __name__ == "__main__":
    unittest.main()
