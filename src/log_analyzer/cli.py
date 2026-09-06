"""Command-line entrypoint and concrete dependency wiring."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Sequence
from datetime import timedelta
import json
from pathlib import Path
import signal
import ssl
import sys
from typing import Any
from urllib.parse import quote

import httpx

from .analysis import NvidiaNimAnalyzer, OnPremAnalyzer, OpenAIResponsesAnalyzer, SecretRedactor
from .config import AppConfig, load_config, read_secret, require_secret
from .errors import (
    AlreadyRunningError,
    ConfigError,
    StorageError,
    UnsupportedPlatformError,
)
from .pipeline import AnalysisPipeline, PipelineInfrastructureError
from .report import ReportWriter
from .run_lock import RunLock
from .source_code import GitSourceResolver, SourceResolutionError
from .sources import ElasticsearchErrorSource, ErrorSource, OracleErrorSource
from .storage import SQLiteStateStore


EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_CONFIG = 2
EXIT_INFRASTRUCTURE = 3
EXIT_INTERNAL = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="log-analyzer",
        description="Analyze Java error logs in a bounded one-shot batch.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("healthcheck", "run"):
        command = commands.add_parser(name)
        command.add_argument(
            "--config",
            required=True,
            type=Path,
            help="Path to the TOML configuration file.",
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    config_path: Path = arguments.config
    try:
        config = load_config(config_path)
    except ConfigError:
        _emit(
            "error",
            "configuration_invalid",
            config_path=str(config_path),
        )
        return EXIT_CONFIG

    lock = RunLock(config.run.lock_file, config_path)
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        _emit(
            "warning",
            "run_skipped_locked",
            owner=exc.metadata,
        )
        return EXIT_OK
    except (UnsupportedPlatformError, OSError):
        _emit("error", "run_lock_unavailable")
        return EXIT_INFRASTRUCTURE

    try:
        try:
            return asyncio.run(_execute(arguments.command, config))
        except ConfigError:
            _emit("error", "runtime_configuration_invalid")
            return EXIT_CONFIG
        except (
            PipelineInfrastructureError,
            StorageError,
            SourceResolutionError,
            OSError,
        ) as exc:
            _emit(
                "error",
                "infrastructure_failure",
                error_type=type(exc).__name__,
            )
            return EXIT_INFRASTRUCTURE
        except KeyboardInterrupt:
            _emit("warning", "run_interrupted")
            return EXIT_PARTIAL
        except Exception as exc:
            _emit(
                "error",
                "internal_failure",
                error_type=type(exc).__name__,
            )
            return EXIT_INTERNAL
    finally:
        lock.release()


async def _execute(command: str, config: AppConfig) -> int:
    openai_key = (
        require_secret(config.openai.api_key_secret)
        if config.openai.auth_required else read_secret(config.openai.api_key_secret)
    )
    if config.error_source.type == "oracle":
        oracle_username = require_secret(config.error_source.username_secret)
        oracle_password = require_secret(config.error_source.password_secret)
        oracle_wallet_password = read_secret(config.error_source.wallet_password_secret)
    else:
        es_api_key = read_secret(config.error_source.api_key_secret)
        es_username = read_secret(config.error_source.username_secret)
        es_password = read_secret(config.error_source.password_secret)
        if es_api_key:
            es_username = None
            es_password = None
        elif (es_username is None) != (es_password is None):
            raise ConfigError(
                "Elasticsearch username and password credentials must be provided together"
            )

    state: SQLiteStateStore | None = None
    source: ErrorSource | None = None
    try:
        state = SQLiteStateStore(
            config.state.path,
            busy_timeout_seconds=config.state.busy_timeout_seconds,
        )
        try:
            if config.error_source.type == "oracle":
                source = OracleErrorSource(
                    config=config.error_source, username=oracle_username, password=oracle_password,
                    wallet_password=oracle_wallet_password,
                    max_text_characters=config.analysis.max_log_characters,
                )
            else:
                source = ElasticsearchErrorSource(
                    url=config.error_source.url,
                    index=config.error_source.index,
                    username=es_username,
                    password=es_password,
                    api_key=es_api_key,
                    ca_certs=(
                        str(config.error_source.tls_ca)
                        if config.error_source.tls_ca is not None
                        else None
                    ),
                    verify_tls=config.error_source.verify_tls,
                    request_timeout=config.error_source.request_timeout_seconds,
                    source_name=config.error_source.name,
                )
        except (ImportError, ValueError, OSError) as exc:
            raise PipelineInfrastructureError(
                "Error source client could not be initialized"
            ) from exc

        services = {service.name: service for service in config.services}
        resolver = GitSourceResolver(
            services,
            context_lines=config.analysis.source_context_lines,
            max_file_bytes=config.analysis.max_source_bytes,
            max_change_context_chars=config.analysis.max_git_change_chars,
        )
        redactor = SecretRedactor()
        analyzer_class = {
            "nvidia_nim": NvidiaNimAnalyzer,
            "onprem": OnPremAnalyzer,
            "openai": OpenAIResponsesAnalyzer,
        }[config.openai.provider]
        provider_options = (
            {
                "structured_output": config.openai.structured_output,
                "enable_thinking": config.openai.enable_thinking,
            }
            if config.openai.provider != "openai"
            else {}
        )
        analyzer = analyzer_class(
            api_key=openai_key,
            model=config.openai.model,
            base_url=config.openai.base_url,
            timeout_seconds=config.openai.timeout_seconds,
            max_output_tokens=config.openai.max_output_tokens,
            redactor=redactor,
            tls_ca=config.openai.tls_ca,
            **provider_options,
        )
        writer = ReportWriter(config.report.directory, redactor=redactor)
        pipeline = AnalysisPipeline(
            source=source,
            state=state,
            services=services,
            source_resolver=resolver,
            analyzer=analyzer,
            report_writer=writer,
            source_name=config.error_source.name,
            model=config.openai.model,
            prompt_version=config.analysis.prompt_version,
            analyzer_version=config.openai.cache_analyzer_version(config.analysis.analyzer_version),
            batch_size=config.run.batch_size,
            max_concurrency=config.run.max_concurrency,
            initial_lookback=timedelta(
                minutes=config.run.initial_lookback_minutes
            ),
            ingestion_delay=timedelta(
                seconds=config.run.ingestion_delay_seconds
            ),
            overlap=timedelta(minutes=config.run.overlap_minutes),
            severities=config.run.severities,
            retention_days=config.report.retention_days,
            max_log_characters=config.analysis.max_log_characters,
            report_directory=config.report.directory,
            redactor=redactor,
        )

        if command == "healthcheck":
            await pipeline.healthcheck()
            await _healthcheck_openai(config, openai_key)
            _emit("info", "healthcheck_succeeded")
            return EXIT_OK
        if command != "run":
            raise ValueError(f"unsupported command: {command}")

        stop_event = asyncio.Event()
        restore_signal = _install_sigterm_handler(stop_event)
        try:
            summary = await pipeline.run(stop_event=stop_event)
        finally:
            restore_signal()
        _emit("info", "run_completed", summary=summary.to_dict())
        return EXIT_PARTIAL if summary.has_failures else EXIT_OK
    finally:
        active_error = sys.exception()
        cleanup_error: BaseException | None = None
        if source is not None:
            try:
                await source.close()
            except Exception as exc:
                cleanup_error = exc
        if state is not None:
            try:
                state.close()
            except Exception as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if active_error is None and cleanup_error is not None:
            raise PipelineInfrastructureError(
                "runtime resources could not be closed"
            ) from cleanup_error


async def _healthcheck_openai(config: AppConfig, api_key: str | None) -> None:
    """Validate authentication and configured model without generating output."""

    model = quote(config.openai.model, safe="")
    base_url = config.openai.base_url.rstrip("/")
    url = (
        f"{base_url}/models"
        if config.openai.provider != "openai"
        else f"{base_url}/models/{model}"
    )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(config.openai.timeout_seconds),
            trust_env=config.openai.provider != "onprem",
            verify=(ssl.create_default_context(cafile=str(config.openai.tls_ca))
                    if config.openai.tls_ca else True),
        ) as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise PipelineInfrastructureError(
            "LLM healthcheck transport failed"
        ) from exc
    if response.status_code >= 400:
        raise PipelineInfrastructureError(
            f"LLM healthcheck failed with HTTP {response.status_code}"
        )
    if config.openai.provider != "openai":
        try:
            body = response.json()
        except ValueError as exc:
            raise PipelineInfrastructureError("LLM model listing was not JSON") from exc
        models = body.get("data") if isinstance(body, dict) else None
        if not isinstance(models, list) or not any(
            isinstance(item, dict) and item.get("id") == config.openai.model
            for item in models
        ):
            raise PipelineInfrastructureError("Configured LLM model was not in the model listing")


def _install_sigterm_handler(stop_event: asyncio.Event) -> Callable[[], None]:
    loop = asyncio.get_running_loop()
    try:
        installed = bool(loop.add_signal_handler(signal.SIGTERM, stop_event.set) is None)
    except (NotImplementedError, RuntimeError, ValueError):
        installed = False

    if not installed:
        return lambda: None

    def restore() -> None:
        try:
            loop.remove_signal_handler(signal.SIGTERM)
        except (NotImplementedError, RuntimeError, ValueError):
            return

    return restore


def _emit(level: str, event: str, **fields: Any) -> None:
    record = {
        "level": level,
        "event": event,
        **fields,
    }
    print(
        json.dumps(
            record,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ),
        file=sys.stderr,
        flush=True,
    )
