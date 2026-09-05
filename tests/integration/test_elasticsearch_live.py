from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from elasticsearch import AsyncElasticsearch

from log_analyzer.analysis.models import AnalysisRequest, AnalysisResult
from log_analyzer.pipeline import AnalysisPipeline
from log_analyzer.report import ReportWriter
from log_analyzer.source_code.git import GitSourceResolver
from log_analyzer.sources.elasticsearch import ElasticsearchErrorSource
from log_analyzer.storage import JobStatus, SQLiteStateStore


ES_URL = os.environ.get("LOG_ANALYZER_TEST_ES_URL")
NOW = datetime(2026, 9, 4, 3, 0, tzinfo=UTC)
INTERNET_FIXTURES = Path(__file__).parents[1] / "fixtures" / "java" / "internet_samples"


class CountingAnalyzer:
    def __init__(self) -> None:
        self.requests: list[AnalysisRequest] = []

    async def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        self.requests.append(request)
        return AnalysisResult(
            summary="The supplied source throws the reported exception.",
            root_causes=[],
            recommended_fixes=[],
            validation_steps=["Run the order service regression test."],
            unknowns=[],
        )


@unittest.skipUnless(
    ES_URL,
    "set LOG_ANALYZER_TEST_ES_URL to run the live Elasticsearch integration test",
)
class LiveElasticsearchPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.repository = self.directory / "orders"
        self.repository.mkdir()
        self._git("init")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "Integration Test")

        source_file = (
            self.repository
            / "src"
            / "main"
            / "java"
            / "com"
            / "example"
            / "order"
            / "OrderService.java"
        )
        source_file.parent.mkdir(parents=True)
        source_file.write_text(
            "package com.example.order;\n"
            "\n"
            "public class OrderService {\n"
            "    public void submit() {\n"
            "        throw new IllegalStateException(\"bad order\");\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        self._git("add", ".")
        self._git("commit", "-m", "add order service")
        self.commit = self._git("rev-parse", "HEAD").stdout.strip()

        self.store = SQLiteStateStore(self.directory / "state.db")
        self.index = f"log-analyzer-test-{uuid.uuid4().hex}"
        self.client: AsyncElasticsearch | None = None

    async def asyncSetUp(self) -> None:
        self.client = AsyncElasticsearch(ES_URL, request_timeout=30)
        await self.client.indices.create(
            index=self.index,
            mappings={
                "properties": {
                    "@timestamp": {"type": "date_nanos"},
                    "event": {"properties": {"id": {"type": "keyword"}}},
                    "log": {"properties": {"level": {"type": "keyword"}}},
                    "service": {
                        "properties": {
                            "name": {"type": "keyword"},
                            "environment": {"type": "keyword"},
                            "version": {"type": "keyword"},
                            "language": {
                                "properties": {"name": {"type": "keyword"}}
                            },
                        }
                    },
                    "git": {
                        "properties": {
                            "commit": {
                                "properties": {"id": {"type": "keyword"}}
                            }
                        }
                    },
                    "error": {
                        "properties": {
                            "type": {"type": "keyword"},
                            "message": {"type": "text"},
                            "stack_trace": {"type": "text", "index": False},
                        }
                    },
                }
            },
        )
        for number in range(2):
            await self.client.index(
                index=self.index,
                id=f"document-{number}",
                document={
                    "@timestamp": (NOW - timedelta(minutes=2, seconds=number)).isoformat(),
                    "event": {"id": f"event-{number}"},
                    "log": {"level": "ERROR"},
                    "service": {
                        "name": "orders",
                        "environment": "test",
                        "version": "1.0.0",
                        "language": {"name": "java"},
                    },
                    "git": {"commit": {"id": self.commit}},
                    "error": {
                        "type": "java.lang.IllegalStateException",
                        "message": "bad order",
                        "stack_trace": (
                            "java.lang.IllegalStateException: bad order\n"
                            "\tat com.example.order.OrderService.submit"
                            "(OrderService.java:5)"
                        ),
                    },
                },
            )
        await self.client.indices.refresh(index=self.index)

    async def asyncTearDown(self) -> None:
        if self.client is not None:
            await self.client.indices.delete(index=self.index, ignore_unavailable=True)
            await self.client.close()

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def _write_java_source(
        self,
        class_name: str,
        line_number: int,
    ) -> None:
        package, _, simple_name = class_name.rpartition(".")
        source_file = (
            self.repository
            / "src"
            / "main"
            / "java"
            / Path(*class_name.split("."))
        ).with_suffix(".java")
        source_file.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        if package:
            lines.append(f"package {package};")
        lines.extend(("", f"public class {simple_name} {{"))
        while len(lines) < line_number - 1:
            lines.append("    // integration fixture padding")
        lines.extend(("    void failingOperation() {}", "}"))
        source_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    async def test_live_es_paging_git_resolution_cache_and_reports(self) -> None:
        assert self.client is not None
        service = SimpleNamespace(
            repository=self.repository,
            source_roots=(PurePosixPath("src/main/java"),),
            application_packages=("com.example.order",),
            framework_packages=("org.springframework",),
        )
        source = ElasticsearchErrorSource(
            url=ES_URL,
            index=self.index,
            source_name="docker-es",
            client=self.client,
        )
        analyzer = CountingAnalyzer()
        reports = self.directory / "reports"
        pipeline = AnalysisPipeline(
            source=source,
            state=self.store,
            services={"orders": service},
            source_resolver=GitSourceResolver({"orders": service}),
            analyzer=analyzer,
            report_writer=ReportWriter(reports),
            source_name="docker-es",
            model="fake-model",
            prompt_version="integration-prompt-1",
            analyzer_version="fake-analyzer-1",
            batch_size=1,
            max_concurrency=1,
            initial_lookback=timedelta(minutes=60),
            ingestion_delay=timedelta(0),
            overlap=timedelta(minutes=5),
            report_directory=reports,
            clock=lambda: NOW,
        )

        summary = await pipeline.run()

        self.assertEqual(summary.fetched_events, 2)
        self.assertEqual(summary.completed, 2)
        self.assertEqual(summary.cache_hits, 1)
        self.assertTrue(summary.checkpoint_saved)
        self.assertEqual(len(analyzer.requests), 1)
        self.assertEqual(len(list(reports.glob("*.md"))), 2)
        self.assertEqual(
            self.store.get_job("docker-es", "event-0").status,
            JobStatus.COMPLETED,
        )
        self.assertEqual(
            self.store.get_job("docker-es", "event-1").status,
            JobStatus.COMPLETED,
        )

    async def test_live_es_without_git_commit_uses_head_blame_and_diff(self) -> None:
        assert self.client is not None
        source_file = (
            self.repository
            / "src"
            / "main"
            / "java"
            / "com"
            / "example"
            / "order"
            / "OrderService.java"
        )
        source_file.write_text(
            "package com.example.order;\n"
            "\n"
            "public class OrderService {\n"
            "    public void submit() {\n"
            "        throw new IllegalStateException(\"fallback order\");\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        self._git("add", ".")
        self._git("commit", "-m", "change order failure for fallback analysis")
        fallback_commit = self._git("rev-parse", "HEAD").stdout.strip()

        await self.client.delete_by_query(
            index=self.index,
            query={"match_all": {}},
            refresh=True,
        )
        await self.client.index(
            index=self.index,
            id="missing-commit-document",
            document={
                "@timestamp": (NOW - timedelta(minutes=2)).isoformat(),
                "event": {"id": "missing-commit-event"},
                "log": {"level": "ERROR"},
                "service": {
                    "name": "orders",
                    "environment": "test",
                    "version": "unknown-deployment",
                    "language": {"name": "java"},
                },
                "error": {
                    "type": "java.lang.IllegalStateException",
                    "message": "fallback order",
                    "stack_trace": (
                        "java.lang.IllegalStateException: fallback order\n"
                        "\tat com.example.order.OrderService.submit"
                        "(OrderService.java:5)"
                    ),
                },
            },
            refresh=True,
        )

        service = SimpleNamespace(
            repository=self.repository,
            branch="HEAD",
            diff_history=3,
            source_roots=(PurePosixPath("src/main/java"),),
            application_packages=("com.example.order",),
            framework_packages=("org.springframework",),
        )
        source = ElasticsearchErrorSource(
            url=ES_URL,
            index=self.index,
            source_name="docker-es-fallback",
            client=self.client,
        )
        analyzer = CountingAnalyzer()
        reports = self.directory / "fallback-reports"
        pipeline = AnalysisPipeline(
            source=source,
            state=self.store,
            services={"orders": service},
            source_resolver=GitSourceResolver({"orders": service}),
            analyzer=analyzer,
            report_writer=ReportWriter(reports),
            source_name="docker-es-fallback",
            model="fake-model",
            prompt_version="integration-prompt-2",
            analyzer_version="fake-analyzer-1",
            batch_size=10,
            max_concurrency=1,
            initial_lookback=timedelta(minutes=60),
            ingestion_delay=timedelta(0),
            overlap=timedelta(minutes=5),
            report_directory=reports,
            clock=lambda: NOW,
        )

        summary = await pipeline.run()

        self.assertEqual(summary.fetched_events, 1)
        self.assertEqual(summary.completed, 1)
        self.assertEqual(summary.no_source, 0)
        self.assertEqual(len(analyzer.requests), 1)
        request = analyzer.requests[0]
        self.assertEqual(request.git_commit, fallback_commit)
        self.assertEqual(request.revision_source, "repository_ref")
        self.assertEqual(request.git_reference, "HEAD")
        self.assertIn(
            "change order failure for fallback analysis",
            request.git_change_context,
        )
        job = self.store.get_job("docker-es-fallback", "missing-commit-event")
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.status, JobStatus.COMPLETED)
        self.assertEqual(job.git_commit, fallback_commit)
        report_files = list(reports.glob("*.md"))
        self.assertEqual(len(report_files), 1)
        report = report_files[0].read_text(encoding="utf-8")
        self.assertIn(r"Source revision basis: repository\_ref", report)
        self.assertIn("change order failure for fallback analysis", report)

    async def test_internet_derived_java_errors_resolve_source_and_write_reports(self) -> None:
        assert self.client is not None
        cases = (
            (
                "null_pointer.txt",
                "java.lang.NullPointerException",
                "MyClass",
                9,
            ),
            (
                "stack_overflow_spring.txt",
                "java.lang.StackOverflowError",
                "com.spectrum.sci.osm.service.OrderDetailsService",
                76,
            ),
            (
                "bean_creation_no_such_method.txt",
                "java.lang.NoSuchMethodError",
                "com.atlassian.bamboo.v2.build.queue.XStreamMessageConverter",
                50,
            ),
            (
                "out_of_memory.txt",
                "java.lang.OutOfMemoryError",
                "com.acme.image.ImageCache",
                118,
            ),
            (
                "sql_constraint.txt",
                "java.sql.SQLIntegrityConstraintViolationException",
                "com.acme.order.OrderRepository",
                87,
            ),
            (
                "class_not_found.txt",
                "java.lang.ClassNotFoundException",
                "com.acme.plugin.PluginLoader",
                34,
            ),
            (
                "completion_exception.txt",
                "java.lang.IllegalStateException",
                "com.acme.payment.PaymentService",
                91,
            ),
        )
        application_packages = (
            "MyClass",
            "com.spectrum.sci.osm.service",
            "com.atlassian.bamboo.v2.build.queue",
            "com.acme.image",
            "com.acme.order",
            "com.acme.plugin",
            "com.acme.payment",
        )

        for _, _, class_name, line_number in cases:
            self._write_java_source(class_name, line_number)
        self._git("add", ".")
        self._git("commit", "-m", "add internet-derived fixture sources")
        commit = self._git("rev-parse", "HEAD").stdout.strip()

        await self.client.delete_by_query(
            index=self.index,
            query={"match_all": {}},
            refresh=True,
        )
        for number, (filename, _, _, _) in enumerate(cases):
            stack_trace = (INTERNET_FIXTURES / filename).read_text(encoding="utf-8")
            await self.client.index(
                index=self.index,
                id=f"internet-document-{number}",
                document={
                    "@timestamp": (NOW - timedelta(minutes=3, seconds=number)).isoformat(),
                    "event": {"id": f"internet-event-{number}"},
                    "log": {"level": "ERROR"},
                    "service": {
                        "name": "internet-java-fixtures",
                        "environment": "test",
                        "version": "1.0.0",
                        "language": {"name": "java"},
                    },
                    "git": {"commit": {"id": commit}},
                    "error": {
                        "type": "java.lang.RuntimeException",
                        "message": f"fixture {filename}",
                        "stack_trace": stack_trace,
                    },
                },
            )
        await self.client.indices.refresh(index=self.index)

        service = SimpleNamespace(
            repository=self.repository,
            source_roots=(PurePosixPath("src/main/java"),),
            application_packages=application_packages,
            framework_packages=("org.springframework",),
        )
        source = ElasticsearchErrorSource(
            url=ES_URL,
            index=self.index,
            source_name="docker-es-internet",
            client=self.client,
        )
        analyzer = CountingAnalyzer()
        reports = self.directory / "internet-reports"
        pipeline = AnalysisPipeline(
            source=source,
            state=self.store,
            services={"internet-java-fixtures": service},
            source_resolver=GitSourceResolver({"internet-java-fixtures": service}),
            analyzer=analyzer,
            report_writer=ReportWriter(reports),
            source_name="docker-es-internet",
            model="fake-model",
            prompt_version="integration-prompt-1",
            analyzer_version="fake-analyzer-1",
            batch_size=2,
            max_concurrency=3,
            initial_lookback=timedelta(minutes=60),
            ingestion_delay=timedelta(0),
            overlap=timedelta(minutes=5),
            report_directory=reports,
            clock=lambda: NOW,
        )

        summary = await pipeline.run()

        self.assertEqual(summary.fetched_events, len(cases))
        self.assertEqual(summary.completed, len(cases))
        self.assertEqual(summary.no_source, 0)
        self.assertEqual(summary.permanent_failures, 0)
        self.assertTrue(summary.checkpoint_saved)
        self.assertEqual(len(analyzer.requests), len(cases))
        self.assertEqual(len(list(reports.glob("*.md"))), len(cases))
        self.assertEqual(
            {request.error_type for request in analyzer.requests},
            {error_type for _, error_type, _, _ in cases},
        )
        self.assertEqual(
            {request.line_number for request in analyzer.requests},
            {line_number for _, _, _, line_number in cases},
        )
        for number in range(len(cases)):
            self.assertEqual(
                self.store.get_job(
                    "docker-es-internet", f"internet-event-{number}"
                ).status,
                JobStatus.COMPLETED,
            )
