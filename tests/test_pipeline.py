from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch

from log_analyzer.analysis.models import AnalysisRequest, AnalysisResult, ErrorPriority
from log_analyzer.analysis.openai_responses import (
    GlobalAnalyzerError,
    InvalidResponseError,
    RetryableAnalyzerError,
)
from log_analyzer.models import ErrorEvent, ErrorPage, SourceContext
from log_analyzer.analysis.redact import SecretRedactor
from log_analyzer.parsers import JavaErrorParser
from log_analyzer.fingerprint import make_fingerprint
from log_analyzer.pipeline import AnalysisPipeline, PipelineInfrastructureError, RunSummary
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


class RunSummaryTests(unittest.TestCase):
    def test_empty_partial_and_interrupted_runs_have_exactly_three_lines(self) -> None:
        for changes in ({}, {"interrupted": True}, {
            "completed": 3, "no_source": 1, "duplicate_events": 2,
            "priority_counts": {"높음": 1, "중간": 2, "낮음": 1},
            "outstanding_retries": 2, "outstanding_permanent_failures": 1,
            "report_cleanup_failures": 1,
        }):
            with self.subTest(changes=changes):
                summary = RunSummary(NOW, NOW, NOW, **changes)
                lines = summary.to_korean_summary(Path("reports\npassword=private-value"))
                self.assertEqual(3, len("\n".join(lines).splitlines()))
                self.assertNotIn("private-value", "\n".join(lines))
                if summary.interrupted:
                    self.assertTrue(lines[0].startswith("처리 중단:"))
                elif summary.completed:
                    self.assertIn("리포트 4건", lines[0])
                    self.assertIn("중복 확인 2건", lines[0])
                    self.assertIn("높음 1건 · 중간 2건 · 낮음 1건", lines[1])
                    self.assertIn("즉시 대응", lines[1])
                    self.assertIn("재시도 대기 2건 · 실패 1건 · 정리 실패 1건", lines[2])
                else:
                    self.assertIn("리포트 0건", lines[0])
                    self.assertIn("분류된 리포트가 없습니다", lines[1])


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_source_reports_count_as_provisional_medium(self) -> None:
        analyzer = Analyzer()
        summary = await self.pipeline(
            PagedSource({None: ErrorPage((event(),), None)}), resolver=Resolver(), analyzer=analyzer,
        ).run()
        self.assertEqual(1, summary.no_source)
        self.assertEqual(0, summary.completed)
        self.assertEqual({"높음": 0, "중간": 1, "낮음": 0}, summary.priority_counts)
        self.assertEqual([], analyzer.requests)
        self.assertIn("소스 미확인 1건", summary.to_korean_summary(self.directory)[0])

    async def test_priority_survives_cache_reuse_and_is_passed_to_report(self) -> None:
        priority = ErrorPriority.unassessed().model_copy(update={"level": "높음"})
        analysis = result().model_copy(update={"error_priority": priority})
        analyzer = Analyzer([analysis])
        writer = Writer(self.directory)
        source = PagedSource({None: ErrorPage((event("priority-1"), event("priority-2")), None)})
        with patch.object(writer, "write", wraps=writer.write) as write:
            summary = await self.pipeline(source, analyzer=analyzer, writer=writer).run()

        self.assertEqual(2, summary.completed)
        self.assertEqual(1, summary.cache_hits)
        self.assertEqual({"높음": 2, "중간": 0, "낮음": 0}, summary.priority_counts)
        self.assertEqual(1, len(analyzer.requests))
        self.assertEqual(2, write.call_count)
        for call in write.call_args_list:
            self.assertEqual("높음", call.args[1].error_priority.level)
            self.assertEqual(priority.response_action, call.args[1].error_priority.response_action)

    async def test_full_method_is_forwarded_without_using_old_partial_source_cache(self) -> None:
        item = event()
        parsed = JavaErrorParser(("com.example.order",)).parse(item)
        self.store.save_analysis_cache(
            fingerprint=make_fingerprint(item, parsed), git_commit=COMMIT,
            model="test-model", prompt_version="prompt-1", analyzer_version="analyzer-1:full-method-v1",
            analysis_json=result().model_dump_json(), now=NOW,
        )
        method = "public void submit() {\n" + "    doWork();\n" * 100 + "}"
        resolver = Resolver(context(source_code=method, context_start_line=1, context_end_line=102))
        analyzer = Analyzer()
        summary = await self.pipeline(
            PagedSource({None: ErrorPage((item,), None)}), resolver=resolver, analyzer=analyzer,
        ).run()

        self.assertEqual(1, summary.completed)
        self.assertEqual(0, summary.cache_hits)
        self.assertEqual(method, analyzer.requests[0].source_code)
        self.assertEqual(1, analyzer.requests[0].context_start_line)
        self.assertEqual(102, analyzer.requests[0].context_end_line)

    async def test_oversized_source_is_not_sent_as_a_partial_method(self) -> None:
        for size in (60_001, 100_001):
            with self.subTest(size=size):
                analyzer = Analyzer()
                source = PagedSource({None: ErrorPage((event(f"oversize-{size}"),), None)})
                with contextlib.redirect_stderr(io.StringIO()):
                    summary = await self.pipeline(
                        source, resolver=Resolver(context(source_code="x" * size)), analyzer=analyzer,
                    ).run()
                self.assertEqual(0, summary.completed)
                self.assertEqual([], analyzer.requests)
                self.assertEqual(JobStatus.PERMANENT_FAILURE, self.store.get_job("logs", f"oversize-{size}").status)

    async def test_long_trace_sends_deepest_cause_to_resolver_and_analysis(self) -> None:
        for outer_frames in (400, 1000):
            with self.subTest(outer_frames=outer_frames):
                trace = (
                    "java.lang.RuntimeException: request failed\n"
                    + "\tat com.example.order.Controller.call(Controller.java:10)\n" * outer_frames
                    + "Caused by: java.sql.SQLException: connection refused\n"
                    + "\tat com.example.order.OrderService.submit(OrderService.java:42)\n"
                )
                item = event(
                    f"deepest-{outer_frames}", stack_trace=trace,
                    environment=f"test-{outer_frames}",
                )
                source = PagedSource({None: ErrorPage((item,), None)})
                analyzer = Analyzer()
                resolver = Resolver(context())
                with patch.object(resolver, "resolve", wraps=resolver.resolve) as resolve:
                    summary = await self.pipeline(source, resolver=resolver, analyzer=analyzer).run()
                self.assertEqual(1, summary.completed)
                selected = resolve.call_args.args[1]
                self.assertEqual("java.sql.SQLException", selected.error_type)
                self.assertEqual(42, selected.frames[0].line_number)
                request = SecretRedactor().redact_request(analyzer.requests[0])
                self.assertEqual("java.sql.SQLException", request.error_type)
                self.assertEqual("connection refused", request.message)
                self.assertLessEqual(len(request.stack_trace), 20_000)
                reparsed = JavaErrorParser().parse(event(stack_trace=request.stack_trace))
                self.assertEqual("java.sql.SQLException", reparsed.error_type)
                self.assertEqual(42, reparsed.frames[0].line_number)

    async def test_retry_and_permanent_failures_are_logged_and_stored_with_location(self) -> None:
        for index, failure in enumerate((RetryableAnalyzerError, InvalidResponseError)):
            event_id = f"diagnostic-{index}"
            source = PagedSource({None: ErrorPage((event(event_id),), None)})
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                await self.pipeline(source, analyzer=Analyzer([failure("password=private-value")])).run()
            records = [json.loads(line) for line in output.getvalue().splitlines()]
            record = next(item for item in records if item.get("event_id") == event_id)
            self.assertEqual("analysis_retry_scheduled" if index == 0 else "analysis_permanent_failure", record["event"])
            self.assertEqual("logs", record["source_name"])
            self.assertEqual("analyze", record["error_location"]["function"])
            self.assertNotIn("private-value", output.getvalue())
            job = self.store.get_job("logs", event_id)
            self.assertIn("test_pipeline.py:", job.last_error)
            self.assertNotIn("private-value", job.last_error)

    async def test_concurrent_failure_keeps_the_failing_event_identity(self) -> None:
        source = PagedSource({None: ErrorPage((event("good"), event("bad")), None)})

        class FailingResolver(Resolver):
            def resolve(self, event, parsed, *, resolved_commit_hint=None):
                if event.event_id == "bad":
                    raise RuntimeError("cannot resolve this event")
                return context()

        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            with self.assertRaises(PipelineInfrastructureError):
                await self.pipeline(source, resolver=FailingResolver()).run()
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        failures = [item for item in records if item["event"] == "event_processing_failed"]
        self.assertEqual(1, len(failures))
        self.assertEqual("bad", failures[0]["event_id"])
        self.assertEqual("orders", failures[0]["service"])
        self.assertEqual("resolve", failures[0]["error_location"]["function"])
        self.assertIsNone(self.store.get_checkpoint("logs"))

    async def test_healthcheck_identifies_the_failing_dependency(self) -> None:
        source = PagedSource({})
        resolver = Resolver()
        pipeline = self.pipeline(source, resolver=resolver)
        for target, method, message in (
            (self.store, "healthcheck", "state database healthcheck failed"),
            (source, "healthcheck", "error source healthcheck failed"),
            (resolver, "healthcheck", "Git repository healthcheck failed"),
        ):
            with patch.object(target, method, side_effect=OSError("unavailable")):
                with self.assertRaisesRegex(PipelineInfrastructureError, message) as raised:
                    await pipeline.healthcheck()
            self.assertIsInstance(raised.exception.__cause__, OSError)

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
            analyzer_version="analyzer-1:full-method-v1:priority-v1:ko-v1",
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
