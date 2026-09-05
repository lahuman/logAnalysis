from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import httpx
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from log_analyzer import cli
from log_analyzer.config import AppConfig
from log_analyzer.errors import AlreadyRunningError, ConfigError
from log_analyzer.pipeline import PipelineInfrastructureError


class FakeLock:
    instances: list["FakeLock"] = []
    acquire_error: BaseException | None = None

    def __init__(self, path, config_path) -> None:
        self.path = path
        self.config_path = config_path
        self.acquired = False
        self.released = False
        self.__class__.instances.append(self)

    def acquire(self):
        if self.__class__.acquire_error is not None:
            raise self.__class__.acquire_error
        self.acquired = True
        return self

    def release(self) -> None:
        self.released = True
        self.acquired = False


def minimal_config() -> SimpleNamespace:
    return SimpleNamespace(
        run=SimpleNamespace(lock_file=Path("/run/log-analyzer/run.lock"))
    )


class CliMainTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeLock.instances.clear()
        FakeLock.acquire_error = None

    def invoke(self, execute) -> tuple[int, dict]:
        stream = io.StringIO()
        with (
            patch.object(cli, "load_config", return_value=minimal_config()),
            patch.object(cli, "RunLock", FakeLock),
            patch.object(cli, "_execute", side_effect=execute),
            contextlib.redirect_stderr(stream),
        ):
            status = cli.main(["run", "--config", "config.toml"])
        output = stream.getvalue().strip()
        return status, json.loads(output) if output else {}

    def test_invalid_configuration_exits_two_without_acquiring_lock(self) -> None:
        stream = io.StringIO()
        with (
            patch.object(cli, "load_config", side_effect=ConfigError("secret")),
            patch.object(cli, "RunLock", FakeLock),
            contextlib.redirect_stderr(stream),
        ):
            status = cli.main(["run", "--config", "bad.toml"])

        self.assertEqual(status, cli.EXIT_CONFIG)
        self.assertEqual(FakeLock.instances, [])
        record = json.loads(stream.getvalue())
        self.assertEqual(record["event"], "configuration_invalid")
        self.assertNotIn("secret", stream.getvalue())

    def test_owned_lock_skips_with_success(self) -> None:
        FakeLock.acquire_error = AlreadyRunningError({"pid": 123})
        status, record = self.invoke(AsyncMock(return_value=cli.EXIT_OK))

        self.assertEqual(status, cli.EXIT_OK)
        self.assertEqual(record["event"], "run_skipped_locked")
        self.assertEqual(record["owner"]["pid"], 123)

    def test_lock_is_acquired_before_execute_and_always_released(self) -> None:
        async def execute(command, config):
            self.assertTrue(FakeLock.instances[0].acquired)
            self.assertEqual(command, "run")
            return cli.EXIT_PARTIAL

        status, record = self.invoke(execute)

        self.assertEqual(status, cli.EXIT_PARTIAL)
        self.assertEqual(record, {})
        self.assertTrue(FakeLock.instances[0].released)

    def test_infrastructure_and_internal_failures_have_distinct_exit_codes(self) -> None:
        async def infrastructure(command, config):
            raise PipelineInfrastructureError("offline")

        status, record = self.invoke(infrastructure)
        self.assertEqual(status, cli.EXIT_INFRASTRUCTURE)
        self.assertEqual(record["event"], "infrastructure_failure")
        self.assertTrue(FakeLock.instances[0].released)

        FakeLock.instances.clear()

        async def internal(command, config):
            raise AssertionError("bug")

        status, record = self.invoke(internal)
        self.assertEqual(status, cli.EXIT_INTERNAL)
        self.assertEqual(record["event"], "internal_failure")
        self.assertTrue(FakeLock.instances[0].released)


class CliExecutionTests(unittest.IsolatedAsyncioTestCase):
    def config(self, directory: Path) -> AppConfig:
        return AppConfig.model_validate(
            {
                "error_source": {
                    "name": "logs",
                    "url": "https://es.example.test",
                    "index": "logs-*",
                },
                "services": [
                    {
                        "name": "orders",
                        "repository": str(directory / "orders.git"),
                        "application_packages": ["com.example.order"],
                    }
                ],
                "openai": {"model": "test-model"},
                "state": {"path": str(directory / "state.db")},
                "report": {"directory": str(directory / "reports")},
            }
        )

    async def test_healthcheck_wires_dependencies_and_closes_resources(self) -> None:
        with tempfile.TemporaryDirectory() as directory_name:
            config = self.config(Path(directory_name))
            state = Mock()
            source = Mock()
            source.close = AsyncMock()
            pipeline = Mock()
            pipeline.healthcheck = AsyncMock()
            pipeline_class = Mock(return_value=pipeline)
            openai_healthcheck = AsyncMock()

            with (
                patch.object(cli, "require_secret", return_value="openai-key"),
                patch.object(cli, "read_secret", return_value=None),
                patch.object(cli, "SQLiteStateStore", return_value=state),
                patch.object(cli, "ElasticsearchErrorSource", return_value=source),
                patch.object(cli, "GitSourceResolver", return_value=Mock()),
                patch.object(cli, "OpenAIResponsesAnalyzer", return_value=Mock()),
                patch.object(cli, "ReportWriter", return_value=Mock()),
                patch.object(cli, "AnalysisPipeline", pipeline_class),
                patch.object(cli, "_healthcheck_openai", openai_healthcheck),
                patch.object(cli, "_emit"),
            ):
                status = await cli._execute("healthcheck", config)

        self.assertEqual(status, cli.EXIT_OK)
        pipeline.healthcheck.assert_awaited_once_with()
        openai_healthcheck.assert_awaited_once_with(config, "openai-key")
        source.close.assert_awaited_once_with()
        state.close.assert_called_once_with()
        self.assertEqual(
            pipeline_class.call_args.kwargs["source_name"],
            "logs",
        )

    async def test_nim_healthcheck_lists_models_and_checks_exact_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(Path(directory))
        from log_analyzer.config import OpenAIConfig
        config = config.model_copy(update={"openai": OpenAIConfig(provider="nvidia_nim", model="vendor/model")})
        for listed, successful in (("vendor/model", True), ("vendor/other", False)):
            requests = []

            def handler(request):
                requests.append(request)
                return httpx.Response(200, json={"data": [{"id": listed}]})

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with patch.object(cli.httpx, "AsyncClient", return_value=client):
                if successful:
                    await cli._healthcheck_openai(config, "test-key")
                else:
                    with self.assertRaises(PipelineInfrastructureError):
                        await cli._healthcheck_openai(config, "test-key")
            self.assertEqual("/v1/models", requests[0].url.path)
            self.assertEqual("Bearer test-key", requests[0].headers["Authorization"])

    async def test_run_selects_nim_and_uses_separate_cache_identity(self) -> None:
        from log_analyzer.config import OpenAIConfig
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(Path(directory)).model_copy(
                update={"openai": OpenAIConfig(provider="nvidia_nim", model="test-model", enable_thinking=False)}
            )
            source = Mock()
            source.close = AsyncMock()
            summary = Mock(has_failures=False)
            pipeline = Mock(run=AsyncMock(return_value=summary))
            with (
                patch.object(cli, "require_secret", return_value="nim-key") as secret,
                patch.object(cli, "read_secret", return_value=None),
                patch.object(cli, "SQLiteStateStore", return_value=Mock()),
                patch.object(cli, "ElasticsearchErrorSource", return_value=source),
                patch.object(cli, "GitSourceResolver", return_value=Mock()),
                patch.object(cli, "NvidiaNimAnalyzer", return_value=Mock()) as nim,
                patch.object(cli, "OpenAIResponsesAnalyzer") as openai,
                patch.object(cli, "ReportWriter", return_value=Mock()),
                patch.object(cli, "AnalysisPipeline", return_value=pipeline) as pipeline_class,
                patch.object(cli, "_emit"),
            ):
                self.assertEqual(cli.EXIT_OK, await cli._execute("run", config))
            secret.assert_called_once_with("NVIDIA_API_KEY")
            openai.assert_not_called()
            self.assertFalse(nim.call_args.kwargs["enable_thinking"])
            self.assertEqual(config.openai.cache_analyzer_version("1"), pipeline_class.call_args.kwargs["analyzer_version"])


if __name__ == "__main__":
    unittest.main()
