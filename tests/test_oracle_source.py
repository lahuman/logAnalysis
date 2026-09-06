from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from pydantic import ValidationError

from log_analyzer import cli
from log_analyzer.config import AppConfig, OracleSourceConfig, load_config
from log_analyzer.errors import ConfigError
from log_analyzer.models import ErrorQuery
from log_analyzer.pipeline import AnalysisPipeline, PipelineInfrastructureError
from log_analyzer.report import ReportWriter
from log_analyzer.sources.oracle import OracleErrorSource, OracleSourceError
from log_analyzer.storage import SQLiteStateStore
from test_pipeline import Analyzer, COMMIT, NOW, Resolver, STACK, context


def settings(**changes):
    return OracleSourceConfig.model_validate(dict(
        type="oracle", name="logs", dsn="tcp://oracle.internal:1521/LOGPDB",
        table="APP_ERROR_LOG", **changes))


def row(identity=1, **changes):
    values = dict(event_id=identity, occurred_at=NOW.replace(tzinfo=None) - timedelta(minutes=2),
                  service="orders", severity="ERROR", message="bad order", stack_trace=STACK,
                  error_type="java.lang.IllegalStateException", environment="production",
                  version="1", git_commit=COMMIT, trace_id=None)
    values.update(changes)
    return tuple(values.values())


class FakeCursor:
    def __init__(self, rows=(), failure=None):
        self.rows = list(rows)
        self.execute = AsyncMock()
        self.close = Mock()
        self.fetch_calls = 0
        self.failure = failure

    async def fetchmany(self, size):
        self.fetch_calls += 1
        if self.failure and self.fetch_calls == 2:
            raise self.failure
        page, self.rows = self.rows[:size], self.rows[size:]
        return page


class OracleConfigTests(unittest.TestCase):
    def test_example_loads_and_legacy_es_default_remains(self):
        root = Path(__file__).resolve().parents[1]
        config = load_config(root / "config/oracle-onprem.toml.example")
        self.assertIsInstance(config.error_source, OracleSourceConfig)
        self.assertEqual(config.error_source.timestamp_timezone, "+09:00")
        config = load_config(root / "config/onprem.toml.example")
        self.assertEqual(config.error_source.type, "elasticsearch")

    def test_rejects_sql_fragments_and_embedded_secrets(self):
        for changes in (
            {"table": "LOGS; DROP TABLE LOGS"}, {"table": "LOGS@REMOTE"},
            {"table": '"MixedCase"'}, {"table": "A.B.C"},
            {"columns": {"message": "SUBSTR(MESSAGE,1,2)"}},
            {"columns": {"occurred_at": ""}}, {"columns": {"unknown": "X"}},
            {"timestamp_timezone": "+14:01"}, {"timestamp_timezone": "Asia/Seoul"},
            {"dsn": "user/secret@host/service"}, {"dsn": "config-https://remote"},
            {"password_secret": "inline-password"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                OracleSourceConfig.model_validate({**settings().model_dump(), **changes})
        config = OracleSourceConfig.model_validate({**settings().model_dump(), "table": "OWNER.LOGS",
                                                   "columns": {"stack_trace": ""}})
        self.assertIsNone(config.columns.stack_trace)


class OracleSourceTests(unittest.IsolatedAsyncioTestCase):
    def make_source(self, rows=(), *, config=None, failure=None, limit=100_000):
        self.cursor = FakeCursor(rows, failure)
        self.connection = Mock(cursor=Mock(return_value=self.cursor), close=AsyncMock())
        self.factory = AsyncMock(return_value=self.connection)
        return OracleErrorSource(config=config or settings(), username="reader", password="test-secret",
                                 connection_factory=self.factory, max_text_characters=limit)

    def query(self, limit=2, severities=("ERROR",)):
        return ErrorQuery(NOW - timedelta(hours=1), NOW, severities, limit)

    async def test_one_select_pages_equal_timestamps_and_large_numeric_ids(self):
        huge = Decimal("12345678901234567890123456789012345678")
        source = self.make_source([row(huge), row("second"), row("third")])
        first = await source.fetch(self.query())
        second = await source.fetch(self.query(), first.next_cursor)
        self.assertEqual([x.event_id for x in (*first.events, *second.events)], [str(huge), "second", "third"])
        self.assertIsNone(second.next_cursor)
        self.cursor.execute.assert_awaited_once()
        self.cursor.close.assert_called_once()
        self.assertEqual(self.cursor.arraysize, 2)
        self.assertEqual(first.events[0].occurred_at, NOW - timedelta(minutes=2))
        await source.close()
        await source.close()
        self.connection.close.assert_awaited_once()

    async def test_full_last_page_requires_empty_fetch_and_stale_tokens_fail(self):
        source = self.make_source([row(1), row(2), row(3), row(4)])
        query = self.query()
        first = await source.fetch(query)
        with self.assertRaises(OracleSourceError):
            await source.fetch(replace(query, limit=3), first.next_cursor)
        second = await source.fetch(query, first.next_cursor)
        self.assertNotEqual(first.next_cursor, second.next_cursor)
        with self.assertRaises(OracleSourceError):
            await source.fetch(query, first.next_cursor)
        final = await source.fetch(query, second.next_cursor)
        self.assertEqual(final.events, ())
        self.assertIsNone(final.next_cursor)
        self.assertEqual(self.cursor.fetch_calls, 3)
        await source.close()

    async def test_bound_sql_and_korean_wall_clock_conversion(self):
        source = self.make_source([row(occurred_at=datetime(2026, 9, 4, 11, 58))],
                                  config=settings(timestamp_timezone="+09:00"))
        attack = "ERROR') OR 1=1 --"
        page = await source.fetch(self.query(severities=(attack, "WARN")))
        args = self.cursor.execute.call_args
        sql, binds = args.args
        self.assertNotIn(attack, sql)
        self.assertIn("LOG_LEVEL IN (:severity_0, :severity_1)", sql)
        self.assertIn("ORDER BY OCCURRED_AT, EVENT_ID", sql)
        self.assertEqual(binds["severity_0"], attack)
        self.assertEqual(binds["started_at"], "2026-09-04T11:00:00.000000")
        self.assertEqual(binds["ended_at"], "2026-09-04T12:00:00.000000")
        self.assertEqual(page.events[0].occurred_at, NOW - timedelta(minutes=2))
        self.assertTrue(args.kwargs["fetch_lobs"])
        self.assertTrue(args.kwargs["fetch_decimals"])
        await source.close()

    async def test_timezone_column_projects_utc_and_uses_explicit_offset_binds(self):
        source = self.make_source([row()], config=settings(timestamp_type="timestamp_tz", timestamp_timezone="+09:00"))
        page = await source.fetch(self.query(severities=()))
        sql, binds = self.cursor.execute.call_args.args
        self.assertIn("SYS_EXTRACT_UTC(OCCURRED_AT)", sql)
        self.assertIn("TO_TIMESTAMP_TZ", sql)
        self.assertNotIn(" IN (", sql)
        self.assertTrue(binds["ended_at"].endswith("+00:00"))
        self.assertEqual(page.events[0].occurred_at, NOW - timedelta(minutes=2))
        await source.close()

    async def test_clob_reads_are_bounded_and_optional_columns_can_be_disabled(self):
        lob = Mock(read=AsyncMock(return_value="한" * 20))
        source = self.make_source([row(message=lob, stack_trace=None)], limit=10,
                                  config=settings(columns={"stack_trace": ""}))
        page = await source.fetch(self.query())
        lob.read.assert_awaited_once_with(offset=1, amount=10)
        self.assertEqual(page.events[0].message, "한" * 10)
        self.assertIsNone(page.events[0].stack_trace)
        self.assertIn("NULL AS stack_trace", self.cursor.execute.call_args.args[0])
        await source.close()

    async def test_bad_row_with_stable_id_is_quarantined_without_raw_data(self):
        source = self.make_source([row(message="private", occurred_at="bad timestamp")])
        page = await source.fetch(self.query())
        self.assertEqual(page.events[0].event_id, "1")
        self.assertTrue(page.events[0].attributes["mapping_error"])
        self.assertNotIn("private", page.events[0].message)
        await source.close()

    async def test_missing_or_inexact_id_stops_snapshot(self):
        for identity in (None, "", "  ", True, Decimal("1.5"), Decimal("NaN"), 1.1):
            with self.subTest(identity=identity):
                source = self.make_source([row(identity)])
                with self.assertRaises(OracleSourceError):
                    await source.fetch(self.query())
                self.cursor.close.assert_called_once()
                await source.close()

    async def test_fetch_and_lob_errors_are_sanitized_and_close_cursor(self):
        for failure in (RuntimeError("password=test-secret"), asyncio.CancelledError()):
            source = self.make_source([row(), row()], failure=failure)
            first = await source.fetch(self.query())
            expected = OracleSourceError if isinstance(failure, Exception) else asyncio.CancelledError
            with self.assertRaises(expected) as error:
                await source.fetch(self.query(), first.next_cursor)
            self.assertNotIn("test-secret", str(error.exception))
            self.cursor.close.assert_called_once()
            await source.close()
        lob = Mock(read=AsyncMock(side_effect=RuntimeError("private lob failure")))
        source = self.make_source([row(message=lob)])
        with self.assertRaises(OracleSourceError):
            await source.fetch(self.query())
        await source.close()

    async def test_healthcheck_uses_projection_without_reading_rows(self):
        source = self.make_source()
        await source.healthcheck()
        self.assertIn("WHERE 1 = 0", self.cursor.execute.call_args.args[0])
        self.assertEqual(self.cursor.fetch_calls, 0)
        self.assertEqual(self.connection.call_timeout, 30000)
        self.assertTrue(self.factory.call_args.kwargs["ssl_server_dn_match"])
        self.cursor.close.assert_called_once()
        await source.close()

    async def test_connect_and_execute_failures_close_resources(self):
        source = self.make_source()
        self.factory.side_effect = RuntimeError("private DSN")
        with self.assertRaises(OracleSourceError):
            await source.healthcheck()
        await source.close()
        self.connection.close.assert_not_called()
        source = self.make_source()
        self.cursor.execute.side_effect = RuntimeError("private SQL")
        with self.assertRaises(OracleSourceError):
            await source.fetch(self.query())
        self.cursor.close.assert_called_once()
        await source.close()
        self.connection.close.assert_awaited_once()


class OraclePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_snapshot_recovers_without_reanalyzing_and_reports_recurrence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteStateStore(root / "state.db")
            cursors = [FakeCursor([row(1), row(2)], RuntimeError("database disconnected")),
                       FakeCursor([row(1), row(2), row(3)])]
            connection = Mock(cursor=Mock(side_effect=cursors), close=AsyncMock())
            source = OracleErrorSource(config=settings(), username="reader", password="test",
                                       connection_factory=AsyncMock(return_value=connection))
            analyzer = Analyzer()
            service = SimpleNamespace(repository=root, source_roots=(PurePosixPath("src/main/java"),),
                                      application_packages=("com.example.order",), framework_packages=("org.springframework",))
            pipeline = AnalysisPipeline(
                source=source, state=store, services={"orders": service}, source_resolver=Resolver(context()),
                analyzer=analyzer, report_writer=ReportWriter(root / "reports"), source_name="logs",
                model="test", prompt_version="test", analyzer_version="test", batch_size=2,
                max_concurrency=2, initial_lookback=timedelta(hours=1), ingestion_delay=timedelta(0),
                overlap=timedelta(minutes=5), report_directory=root / "reports", clock=lambda: NOW)
            try:
                with self.assertRaises(PipelineInfrastructureError):
                    await pipeline.run()
                self.assertIsNone(store.get_checkpoint("logs"))
                self.assertEqual(len(analyzer.requests), 1)
                self.assertEqual(len(list((root / "reports").rglob("*.md"))), 2)
                summary = await pipeline.run()
                self.assertTrue(summary.checkpoint_saved)
                self.assertEqual(store.get_checkpoint("logs").ended_at, NOW)
                self.assertEqual(len(analyzer.requests), 1)
                self.assertEqual(len(list((root / "reports").rglob("*.md"))), 3)
                for cursor in cursors:
                    cursor.execute.assert_awaited_once()
            finally:
                await source.close()
                store.close()


class OracleCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_oracle_selection_requires_only_oracle_and_llm_secrets(self):
        root = Path(__file__).resolve().parents[1]
        config = load_config(root / "config/oracle-onprem.toml.example")
        source = Mock(close=AsyncMock())
        summary = Mock(has_failures=False)
        summary.to_dict.return_value = {}
        pipeline = Mock(run=AsyncMock(return_value=summary))
        with (
            patch.object(cli, "require_secret", side_effect=lambda name: name + "-test") as required,
            patch.object(cli, "read_secret", return_value=None) as optional,
            patch.object(cli, "SQLiteStateStore", return_value=Mock()),
            patch.object(cli, "OracleErrorSource", return_value=source) as oracle,
            patch.object(cli, "ElasticsearchErrorSource") as es,
            patch.object(cli, "GitSourceResolver", return_value=Mock()),
            patch.object(cli, "OnPremAnalyzer", return_value=Mock()),
            patch.object(cli, "ReportWriter", return_value=Mock()),
            patch.object(cli, "AnalysisPipeline", return_value=pipeline),
            patch.object(cli, "_emit"),
        ):
            self.assertEqual(await cli._execute("run", config), cli.EXIT_OK)
        self.assertEqual(required.call_args_list, [call("LLM_API_KEY"), call("ORACLE_USERNAME"), call("ORACLE_PASSWORD")])
        optional.assert_called_once_with("ORACLE_WALLET_PASSWORD")
        es.assert_not_called()
        self.assertEqual(oracle.call_args.kwargs["password"], "ORACLE_PASSWORD-test")
        source.close.assert_awaited_once()

    async def test_missing_oracle_password_fails_before_opening_state_or_source(self):
        config = load_config(Path(__file__).resolve().parents[1] / "config/oracle-onprem.toml.example")
        with (patch.object(cli, "require_secret", side_effect=["llm-test", "reader", ConfigError("missing")]),
              patch.object(cli, "SQLiteStateStore") as state,
              patch.object(cli, "OracleErrorSource") as source):
            with self.assertRaises(ConfigError):
                await cli._execute("run", config)
        state.assert_not_called()
        source.assert_not_called()


if __name__ == "__main__":
    unittest.main()
