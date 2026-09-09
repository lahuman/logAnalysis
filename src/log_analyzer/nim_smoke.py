"""Opt-in NIM generation using synthetic Java context, without ES or batch state."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tomllib
from uuid import uuid4

from pydantic import ValidationError

from .analysis import AnalyzerError, NvidiaNimAnalyzer, OnPremAnalyzer, SecretRedactor
from .analysis.models import AnalysisRequest
from .analysis.openai_responses import InvalidResponseError, PROMPT_VERSION
from .config import ConfigError, OpenAIConfig, read_secret, require_secret
from .diagnostics import emit, error_details
from .report import ReportWriter


def smoke_request() -> AnalysisRequest:
    return AnalysisRequest(
        service="nim-smoke",
        environment="synthetic-test",
        version="synthetic-fixture-1",
        fingerprint="nim-smoke-null-customer-1",
        error_type="java.lang.NullPointerException",
        message='Cannot invoke "String.trim()" because "customer" is null',
        stack_trace=(
            'java.lang.NullPointerException: Cannot invoke "String.trim()" because "customer" is null\n'
            "\tat com.example.smoke.OrderService.customerName(OrderService.java:4)"
        ),
        git_commit="synthetic-fixture-not-a-deployment",
        source_path="src/main/java/com/example/smoke/OrderService.java",
        line_number=4,
        function_name="customerName",
        class_name="com.example.smoke.OrderService",
        source_code=(
            "package com.example.smoke;\n"
            "public final class OrderService {\n"
            "    public String customerName(String customer) {\n"
            "        return customer.trim();\n"
            "    }\n"
            "}"
        ),
        context_start_line=1,
        context_end_line=6,
        revision_source="repository_ref",
        git_reference="synthetic-fixture",
        git_change_context="합성 테스트 소스입니다. 실제 Git 변경 이력이나 배포 정보는 포함하지 않습니다.",
    )


async def run_smoke(
    config: OpenAIConfig,
    output_directory: Path,
    credentials_directory: Path | None = None,
) -> Path:
    if config.provider not in {"nvidia_nim", "onprem"}:
        raise ConfigError("smoke requires provider=nvidia_nim or onprem")
    environment = dict(os.environ)
    if credentials_directory is not None:
        environment["CREDENTIALS_DIRECTORY"] = str(credentials_directory.resolve())
    api_key = (require_secret(config.api_key_secret, environment) if config.auth_required
               else read_secret(config.api_key_secret, environment))
    redactor = SecretRedactor()
    analyzer_class = OnPremAnalyzer if config.provider == "onprem" else NvidiaNimAnalyzer
    analyzer = analyzer_class(
        api_key=api_key,
        model=config.model,
        base_url=config.base_url,
        timeout_seconds=config.timeout_seconds,
        max_output_tokens=config.max_output_tokens,
        structured_output=config.structured_output,
        enable_thinking=config.enable_thinking,
        max_attempts=1,
        redactor=redactor,
        tls_ca=config.tls_ca,
    )
    request = smoke_request()
    result = redactor.redact_result(await analyzer.analyze(request))
    if (
        not any(cause.evidence for cause in result.root_causes)
        or not result.recommended_fixes
        or not result.validation_steps
    ):
        raise InvalidResponseError("Smoke analysis lacked source evidence, fixes or validation steps")
    now = datetime.now(UTC)
    return ReportWriter(output_directory, redactor=redactor).write(
        request,
        result,
        event_id=f"nim-smoke-{uuid4().hex}",
        source_name="onprem-smoke" if config.provider == "onprem" else "nvidia-nim-smoke",
        model=config.model,
        prompt_version=PROMPT_VERSION,
        first_seen=now,
        last_seen=now,
        analyzed_at=now,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--credentials-directory", type=Path)
    parser.add_argument("--output-directory", type=Path, default=Path("reports/nim-smoke"))
    args = parser.parse_args(argv)
    try:
        with args.config.open("rb") as stream:
            data = tomllib.load(stream)
        config = OpenAIConfig.model_validate(data["openai"])
        report = asyncio.run(run_smoke(config, args.output_directory, args.credentials_directory))
    except (ConfigError, ValidationError, KeyError, tomllib.TOMLDecodeError) as exc:
        emit("error", "nim_smoke_configuration_invalid", error=exc, config_path=str(args.config))
        return 2
    except AnalyzerError as exc:
        emit("error", "nim_smoke_failed", error=exc, reason=error_details(exc)["error_message"])
        return 1
    except OSError as exc:
        emit("error", "nim_smoke_io_failed", error=exc, config_path=str(args.config))
        return 3
    print(json.dumps({"event": "nim_smoke_succeeded", "model": config.model, "report": str(report.resolve())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
