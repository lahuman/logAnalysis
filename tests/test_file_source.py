from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import tempfile
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from pydantic import ValidationError

from log_analyzer import cli
from log_analyzer.config import FileSourceConfig, load_config
from log_analyzer.models import ErrorQuery
from log_analyzer.parsers import JavaErrorParser
from log_analyzer.pipeline import AnalysisPipeline, PipelineInfrastructureError
from log_analyzer.report import ReportWriter
from log_analyzer.sources.file import FileErrorSource, FileSourceError
from log_analyzer.storage import SQLiteStateStore
from tests.test_pipeline import Analyzer, Resolver, context


NOW = datetime(2026, 9, 7, 4, tzinfo=UTC)
RECORD = (
    "2026-09-07 09:01:00.123 ERROR [worker] Logger - bad order\n"
    "java.lang.IllegalStateException: bad order\n"
    "\tat com.example.order.OrderService.submit(OrderService.java:42)\n"
    "Caused by: java.lang.NullPointerException: customer missing\n"
    "\tat com.example.order.OrderService.validate(OrderService.java:30)\n"
)


class FileSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.path = Path(self.work.name) / "application.log"
        self.path.write_text(RECORD, encoding="utf-8")
        self.query = ErrorQuery(NOW - timedelta(minutes=10), NOW, ("ERROR", "FATAL"), 1)

    def source(self, **changes):
        config = FileSourceConfig(type="file", name="logs", path=self.path, service="orders", **changes)
        source = FileErrorSource(config=config)
        self.addAsyncCleanup(source.close)
        return source

    async def test_multiline_causes_info_boundaries_and_paging(self):
        self.path.write_text(RECORD + "2026-09-07 09:02:00 INFO Logger - done\n" + RECORD,
                             encoding="utf-8")
        source = self.source()
        first = await source.fetch(self.query)
        second = await source.fetch(self.query, first.next_cursor)
        last = await source.fetch(self.query, second.next_cursor)
        self.assertEqual(len(first.events), 1)
        self.assertEqual(len(second.events), 1)
        self.assertIsNone(last.next_cursor)
        self.assertFalse(last.events)
        self.assertNotEqual(first.events[0].event_id, second.events[0].event_id)
        event = first.events[0]
        self.assertEqual(event.occurred_at, datetime(2026, 9, 7, 0, 1, 0, 123000, tzinfo=UTC))
        self.assertNotIn("Logger - done", event.stack_trace)
        parsed = JavaErrorParser(("com.example.order",)).parse(event)
        self.assertTrue(any(f.line_number == 30 for f in parsed.frames))
        self.assertIn("java.lang.NullPointerException", str(parsed.cause_chain))

    async def test_private_snapshot_excludes_appends_and_replacement_until_next_run(self):
        source = self.source()
        first = await source.fetch(self.query)
        with self.path.open('a', encoding='utf-8') as output:
            output.write(RECORD)
        self.assertFalse((await source.fetch(self.query, first.next_cursor)).events)
        replay = await source.fetch(replace(self.query, limit=10))
        self.assertEqual(len(replay.events), 2)
        self.assertEqual(first.events[0].event_id, replay.events[0].event_id)
        first = await source.fetch(self.query)
        self.path.write_text('new invalid file', encoding='utf-8')
        second = await source.fetch(self.query, first.next_cursor)
        self.assertEqual(replay.events[1].event_id, second.events[0].event_id)

    async def test_old_logs_default_and_optional_time_filter(self):
        self.assertEqual(len((await self.source().fetch(self.query)).events), 1)
        self.assertFalse((await self.source(filter_time_window=True).fetch(self.query)).events)
        query = replace(self.query, started_at=datetime(2026, 9, 6, tzinfo=UTC))
        self.assertEqual(len((await self.source(filter_time_window=True).fetch(query)).events), 1)

    async def test_utf8_bom_cp949_and_explicit_offset(self):
        self.path.write_bytes(b'\xef\xbb\xbf'+RECORD.replace('09:01:00.123', '09:01:00,123+09:00').encode())
        self.assertEqual((await self.source().fetch(self.query)).events[0].occurred_at.hour, 0)
        self.path.write_bytes(RECORD.replace('bad order', '주문 오류').encode('cp949'))
        self.assertIn('주문 오류', (await self.source(encoding='cp949').fetch(self.query)).events[0].message)
        with self.assertRaises(FileSourceError):
            await self.source().fetch(self.query)

    async def test_custom_header_and_config_validation(self):
        self.path.write_text('[2026-09-07T00:01:00Z] [ERROR] bad order\n', encoding='utf-8')
        pattern = r'^\[(?P<timestamp>[^\]]+)\] \[(?P<severity>\w+)\] (?P<message>.*)$'
        self.assertEqual((await self.source(header_pattern=pattern).fetch(self.query)).events[0].message, 'bad order')
        for values in ({'header_pattern':'['}, {'header_pattern':'.*'}, {'timestamp_timezone':'+14:30'}, {'service':' '}):
            with self.assertRaises(ValidationError):
                FileSourceConfig.model_validate(dict(type='file', path=self.path, service='orders') | values)

    async def test_invalid_header_date_encoding_and_limits_never_echo_log(self):
        cases = [('secret-header\n', {}), (RECORD.replace('09:01:00', '99:01:00'), {}),
                 (RECORD, {'max_record_bytes':50}), (RECORD, {'max_file_bytes':10})]
        for text, config in cases:
            self.path.write_text(text, encoding='utf-8')
            with self.assertRaises(FileSourceError) as result:
                await self.source(**config).fetch(self.query)
            self.assertNotIn('secret-header', str(result.exception))
            self.assertNotIn('customer missing', str(result.exception))

    async def test_missing_directory_empty_and_healthcheck_cursor(self):
        source = self.source()
        first = await source.fetch(self.query)
        await source.healthcheck()
        self.assertFalse((await source.fetch(self.query, first.next_cursor)).events)
        self.path.write_bytes(b'')
        with self.assertRaises(FileSourceError):
            await source.healthcheck()
        self.path.unlink()
        with self.assertRaises(FileSourceError):
            await source.fetch(self.query)
        self.path.mkdir()
        with self.assertRaises(FileSourceError):
            await source.fetch(self.query)

    async def test_stale_and_wrong_query_cursor_rejected(self):
        source = self.source()
        first = await source.fetch(self.query)
        with self.assertRaises(FileSourceError):
            await source.fetch(replace(self.query, limit=2), first.next_cursor)
        await source.close()
        with self.assertRaises(FileSourceError):
            await source.fetch(self.query, first.next_cursor)

    async def test_snapshot_change_during_copy_fails_and_closes(self):
        before = self.path.stat()
        after = SimpleNamespace(st_size=before.st_size+1, st_mtime_ns=before.st_mtime_ns, st_ctime_ns=before.st_ctime_ns)
        with patch('log_analyzer.sources.file.os.fstat', side_effect=[before, after]):
            with self.assertRaisesRegex(FileSourceError, 'changed'):
                await self.source().fetch(self.query)

    async def test_pipeline_replay_append_and_failure_recovery(self):
        root = Path(self.work.name)
        store = SQLiteStateStore(root / 'state.db')
        self.addCleanup(store.close)
        source = self.source()
        analyzer = Analyzer()
        service = SimpleNamespace(repository=root, source_roots=(PurePosixPath('src/main/java'),),
                                  application_packages=('com.example.order',), framework_packages=('org.springframework',))
        pipeline = AnalysisPipeline(source=source, state=store, services={'orders':service},
            source_resolver=Resolver(context()), analyzer=analyzer, report_writer=ReportWriter(root/'reports'),
            source_name='logs', model='test', prompt_version='test', analyzer_version='test',
            batch_size=1, ingestion_delay=timedelta(0), report_directory=root/'reports', clock=lambda: NOW)
        first = await pipeline.run()
        replay = await pipeline.run()
        self.assertEqual(first.completed, 1)
        self.assertEqual(replay.duplicate_events, 1)
        self.path.write_text(RECORD+RECORD, encoding='utf-8')
        appended = await pipeline.run()
        self.assertEqual(appended.completed, 1)
        self.assertEqual(appended.cache_hits, 1)
        self.assertEqual(len(analyzer.requests), 1)
        self.assertEqual(len(list((root/'reports').rglob('*.md'))), 2)
        checkpoint = store.get_checkpoint('logs')
        self.path.write_text(RECORD+'2026-09-07 99:00:00 ERROR private bad header\n', encoding='utf-8')
        with self.assertRaises(PipelineInfrastructureError):
            await pipeline.run()
        self.assertEqual(store.get_checkpoint('logs'), checkpoint)
        self.path.write_text(RECORD+RECORD, encoding='utf-8')
        self.assertTrue((await pipeline.run()).checkpoint_saved)
        self.assertEqual(len(analyzer.requests), 1)


class FileCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_selection_does_not_require_database_credentials(self):
        config = load_config(Path(__file__).resolve().parents[1] / 'config/file-onprem.toml.example')
        source = Mock(close=AsyncMock())
        summary = Mock(has_failures=False)
        summary.to_dict.return_value = {}
        with (patch.object(cli,'require_secret',return_value='llm-test') as required,
              patch.object(cli,'read_secret') as optional,
              patch.object(cli,'SQLiteStateStore'), patch.object(cli,'FileErrorSource',return_value=source) as local,
              patch.object(cli,'OracleErrorSource') as oracle, patch.object(cli,'ElasticsearchErrorSource') as es,
              patch.object(cli,'GitSourceResolver'), patch.object(cli,'OnPremAnalyzer'), patch.object(cli,'ReportWriter'),
              patch.object(cli,'AnalysisPipeline',return_value=Mock(run=AsyncMock(return_value=summary))), patch.object(cli,'_emit')):
            self.assertEqual(await cli._execute('run',config),cli.EXIT_OK)
        self.assertEqual(required.call_args_list,[call('LLM_API_KEY')])
        optional.assert_not_called()
        oracle.assert_not_called()
        es.assert_not_called()
        local.assert_called_once()
        source.close.assert_awaited_once()
