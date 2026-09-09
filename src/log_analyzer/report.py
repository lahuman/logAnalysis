"""Safe, deterministic Markdown report rendering."""

from __future__ import annotations

import hashlib
import html
import os
import re
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from string import Template

from .analysis.models import AnalysisRequest, AnalysisResult, ErrorPriority, validated_evidence
from .analysis.openai_responses import PROMPT_VERSION
from .analysis.redact import SecretRedactor


_FALLBACK_TEMPLATE = """# Java 오류 분석 리포트

- 처리 상태: ${status}
- 이벤트: ${event_id}
- 서비스: ${service}
- 환경: ${environment}
- 버전: ${version}
- 분석한 Git 커밋: ${git_commit}
- 소스 버전 기준: ${revision_source}
- Git 참조: ${git_reference}
- 발생 횟수: ${occurrence_count}
- 최초 발생 시각: ${first_seen}
- 최근 발생 시각: ${last_seen}
- 분석 시각: ${analyzed_at}
- 분석 모델: ${model}
- 프롬프트 버전: ${prompt_version}

## 오류 수준 및 대응 우선순위

${error_priority}

## 오류 정보

- 오류 유형: ${error_type}
- 원문 메시지: ${message}

## 대표 스택 트레이스

```text
${stack_trace}
```

## 확인된 소스 위치

${source_location}

## Git 변경 이력

${git_change_context}

## 분석 요약

${summary}

## 근거에 따른 원인 후보

${root_causes}

## 권장 수정 방법

${recommended_fixes}

## 검증 및 회귀 테스트

${validation_steps}

## 추가 확인 사항

${unknowns}
"""

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


class ReportWriter:
    def __init__(
        self,
        output_dir: str | Path,
        *,
        template_path: str | Path | None = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self._output_dir = Path(output_dir)
        self._redactor = redactor or SecretRedactor()
        self._template = Template(self._load_template(template_path))

    def write(
        self,
        request: AnalysisRequest,
        result: AnalysisResult,
        *,
        event_id: str,
        source_name: str = "",
        occurrence_count: int = 1,
        first_seen: datetime | None = None,
        last_seen: datetime | None = None,
        model: str | None = None,
        prompt_version: str | None = PROMPT_VERSION,
        analyzed_at: datetime | None = None,
        status: str = "COMPLETED",
    ) -> Path:
        if occurrence_count < 1:
            raise ValueError("occurrence_count must be positive")
        safe = self._redactor.redact_request(request)
        result = validated_evidence(result, safe)
        redact = self._redactor.redact_text
        values = self._common_values(
            event_id=event_id,
            service=safe.service,
            environment=safe.environment,
            version=safe.version,
            git_commit=safe.git_commit,
            error_type=safe.error_type,
            message=safe.message,
            stack_trace=safe.stack_trace,
            occurrence_count=occurrence_count,
            first_seen=first_seen,
            last_seen=last_seen,
            analyzed_at=analyzed_at,
            status=status,
            model=model,
            prompt_version=prompt_version,
        )
        values.update(
            error_level=result.error_priority.level,
            error_priority=_error_priority(result.error_priority, redact),
            source_location=(
                f"`{_inline_code(safe.source_path)}:{safe.line_number}`"
                + (
                    f" (`{_inline_code(safe.function_name)}`)"
                    if safe.function_name
                    else ""
                )
            ),
            revision_source={"event_commit": "로그에 기록된 배포 커밋", "repository_ref": "설정한 Git 참조의 소스"}[safe.revision_source],
            git_reference=_markdown_text(safe.git_reference or "-"),
            git_change_context=(
                "```text\n"
                + _fenced_text(redact(safe.git_change_context))
                + "\n```"
                if safe.git_change_context
                else "- 로그에 배포 커밋이 지정되어 변경 이력을 별도로 수집하지 않았습니다."
            ),
            summary=_markdown_text(redact(result.summary)),
            root_causes=_root_causes(result, redact),
            recommended_fixes=_recommended_fixes(result, redact),
            validation_steps=_string_list(result.validation_steps, redact),
            unknowns=_string_list(result.unknowns, redact),
            three_line_summary=_three_line_summary(
                f"{result.error_priority.level} — {result.summary}",
                result.error_priority.impact, result.error_priority.response_action, redact,
            ),
        )
        return self._atomic_write(
            event_id,
            self._render(values),
            source_name=source_name,
        )

    def write_no_source(
        self,
        *,
        event_id: str,
        source_name: str = "",
        service: str,
        environment: str,
        version: str,
        git_commit: str,
        error_type: str,
        message: str,
        stack_trace: str,
        reason: str,
        occurrence_count: int = 1,
        first_seen: datetime | None = None,
        last_seen: datetime | None = None,
        analyzed_at: datetime | None = None,
    ) -> Path:
        if occurrence_count < 1:
            raise ValueError("occurrence_count must be positive")
        redact = self._redactor.redact_text
        values = self._common_values(
            event_id=event_id,
            service=redact(service),
            environment=redact(environment),
            version=redact(version),
            git_commit=redact(git_commit),
            error_type=redact(error_type),
            message=_bounded(redact(message), 4_000),
            stack_trace=_bounded(redact(stack_trace), 20_000),
            occurrence_count=occurrence_count,
            first_seen=first_seen,
            last_seen=last_seen,
            analyzed_at=analyzed_at,
            status="NO_SOURCE",
            model=None,
            prompt_version=None,
        )
        values.update(
            error_level="중간",
            error_priority=_error_priority(
                ErrorPriority.unassessed().model_copy(update={
                    "rationale": "소스를 확인하지 못해 LLM 분석을 수행하지 않았습니다. 영향 확인 전까지 중간으로 잠정 분류합니다.",
                }), redact,
            ),
            source_location="소스를 확인하지 못해 LLM 분석을 수행하지 않았습니다.",
            revision_source="확인 불가",
            git_reference="-",
            git_change_context="- 확인할 수 있는 Git 변경 이력이 없습니다.",
            summary=_markdown_text(redact(reason)),
            root_causes="- 소스 근거가 없어 원인을 분석하지 못했습니다.",
            recommended_fixes="- 배포 커밋과 소스 경로 설정을 확인한 뒤 다시 분석하세요.",
            validation_steps="- 서비스의 Git 저장소, 커밋, 소스 루트 설정을 확인하세요.",
            unknowns="- 일치하는 소스를 확보하기 전까지 근본 원인은 확인할 수 없습니다.",
            three_line_summary=_three_line_summary(
                "중간·잠정 판단 — 소스 미확인으로 원인 분석을 수행하지 못했습니다.",
                "서비스와 데이터 영향을 먼저 확인해야 합니다.",
                "서비스 상태와 배포 커밋·소스 경로를 확인한 뒤 다시 분석하세요.", redact,
            ),
        )
        return self._atomic_write(
            event_id,
            self._render(values),
            source_name=source_name,
        )

    def _render(self, values: dict[str, str]) -> str:
        contents = self._template.substitute(values)
        if "error_priority" not in self._template.get_identifiers():
            # Older custom templates must also show the level and response guidance.
            section = "## 오류 수준 및 대응 우선순위\n\n" + values["error_priority"] + "\n\n"
            first, separator, rest = contents.partition("\n")
            if first.startswith("# ") and separator:
                contents = first + "\n\n" + section + rest.lstrip("\n")
            else:
                contents = section + contents
        return contents.rstrip() + "\n\n## 세 줄 요약\n\n" + values["three_line_summary"] + "\n"

    def _common_values(
        self,
        *,
        event_id: str,
        service: str,
        environment: str,
        version: str,
        git_commit: str,
        error_type: str,
        message: str,
        stack_trace: str,
        occurrence_count: int,
        first_seen: datetime | None,
        last_seen: datetime | None,
        analyzed_at: datetime | None,
        status: str,
        model: str | None,
        prompt_version: str | None,
    ) -> dict[str, str]:
        analyzed_at = analyzed_at or datetime.now(timezone.utc)
        return {
            "status": _markdown_text({"COMPLETED": "분석 완료", "NO_SOURCE": "소스 미확인", "SYNTHETIC_EXAMPLE": "합성 예시"}.get(status, status)),
            "event_id": _markdown_text(self._redactor.redact_text(event_id)),
            "service": _markdown_text(service),
            "environment": _markdown_text(environment),
            "version": _markdown_text(version),
            "git_commit": _markdown_text(git_commit),
            "occurrence_count": str(occurrence_count),
            "first_seen": _format_datetime(first_seen),
            "last_seen": _format_datetime(last_seen),
            "analyzed_at": _format_datetime(analyzed_at),
            "model": _markdown_text(model or "-"),
            "prompt_version": _markdown_text(prompt_version or "-"),
            "error_type": _markdown_text(error_type),
            "message": _markdown_text(message),
            "stack_trace": _fenced_text(stack_trace),
        }

    def _atomic_write(
        self, event_id: str, contents: str, *, source_name: str = ""
    ) -> Path:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        destination = self._output_dir / _report_filename(event_id, source_name)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=self._output_dir
        )
        temporary = Path(temporary_name)
        try:
            try:
                os.fchmod(descriptor, 0o600)
            except (AttributeError, OSError):
                pass
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                descriptor = -1
                handle.write(contents)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            return destination
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _load_template(template_path: str | Path | None) -> str:
        if template_path is not None:
            return Path(template_path).read_text(encoding="utf-8")
        repository_template = Path(__file__).resolve().parents[2] / "templates" / "report.md"
        try:
            return repository_template.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _FALLBACK_TEMPLATE


def _report_filename(event_id: str, source_name: str = "") -> str:
    identity = f"{source_name}\0{event_id}"
    slug_value = f"{source_name}-{event_id}" if source_name else event_id
    slug = _UNSAFE_FILENAME.sub("-", slug_value).strip(".-_")[:64] or "event"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}.md"


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _markdown_text(value: str) -> str:
    value = "".join(char for char in value if char in "\n\t" or ord(char) >= 32)
    value = html.escape(value, quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>-])", r"\\\1", value)


def _inline_code(value: str) -> str:
    return value.replace("`", "\uff40").replace("\r", " ").replace("\n", " ")


def _fenced_text(value: str) -> str:
    return value.replace("```", "` ` `")


def _bounded(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    marker = "\n[TRUNCATED]"
    return value[: maximum - len(marker)] + marker


def _error_priority(priority: ErrorPriority, redact: Callable[[str], str]) -> str:
    guidance = {
        "높음": (
            "긴급 대응", "즉시 영향 확인과 대응을 시작하세요.",
            "담당자에게 즉시 공유하고 피해 확산 방지와 서비스 복구를 우선하세요. 복구 후 원인 수정과 회귀 검증을 수행하세요.",
        ),
        "중간": (
            "우선 점검", "당일 업무 시간 내 영향을 점검하고 수정 일정을 확정하세요.",
            "재현 조건과 영향 범위를 확인하고 임시 대응 후 수정·검증 계획을 세우세요. 영향이 지속되거나 커지면 즉시 상향하세요.",
        ),
        "낮음": (
            "계획 대응", "모니터링을 유지하며 다음 정기 개선 일정에 반영하세요.",
            "이슈를 등록하고 재현·회귀 테스트와 함께 개선하세요. 반복 증가나 사용자 영향이 확인되면 우선순위를 다시 평가하세요.",
        ),
    }
    urgency, timing, handling = guidance[priority.level]
    status = "잠정 판단 · 영향 확인 필요" if priority.provisional else "제공된 근거에 따른 판단"
    return (
        f"**오류 수준: {priority.level}** · {urgency}\n\n"
        f"- 판단 상태: {status}\n"
        f"- 권장 처리 시점: {timing}\n"
        f"- 판단 근거: {_markdown_text(redact(priority.rationale))}\n"
        f"- 서비스·데이터 영향: {_markdown_text(redact(priority.impact))}\n"
        f"- 우선 대응: {_markdown_text(redact(priority.response_action))}\n"
        f"- 수정·검증 순서: {handling}\n"
        f"- 상향·긴급 재평가 조건: {_markdown_text(redact(priority.escalation_condition))}\n\n"
        "처리 시점은 권장 기준입니다. 실제 장애 영향과 조직의 운영 기준에 따라 최종 우선순위를 결정하세요."
    )


def _root_causes(result: AnalysisResult, redact: Callable[[str], str]) -> str:
    if not result.root_causes:
        return "- 근거가 확인된 원인 후보가 없습니다."
    sections: list[str] = []
    for index, cause in enumerate(result.root_causes, start=1):
        sections.append(
            f"{index}. {_markdown_text(redact(cause.cause))} "
            f"(분석 확신도: {cause.confidence:.2f})"
        )
        for evidence in cause.evidence:
            sections.append(
                "   - 근거: "
                f"`{_inline_code(redact(evidence.file))}:{evidence.line}` - "
                f"{_markdown_text(redact(evidence.description))}"
            )
        if not cause.evidence:
            sections.append("   - 근거: 제공된 소스 범위에서 확인된 근거가 없습니다.")
    return "\n".join(sections)


def _recommended_fixes(
    result: AnalysisResult, redact: Callable[[str], str]
) -> str:
    if not result.recommended_fixes:
        return "- 권장 수정안이 없습니다."
    sections: list[str] = []
    for fix in result.recommended_fixes:
        files = (
            ", ".join(f"`{_inline_code(redact(path))}`" for path in fix.files)
            or "없음"
        )
        risk = {"low": "낮음", "medium": "중간", "high": "높음"}[fix.risk]
        sections.append(
            f"- {_markdown_text(redact(fix.description))} "
            f"(변경 위험도: {risk}; 대상 파일: {files})"
        )
    return "\n".join(sections)


def _string_list(values: list[str], redact: Callable[[str], str]) -> str:
    if not values:
        return "- 없음."
    return "\n".join(f"- {_markdown_text(redact(value))}" for value in values)


def _three_line_summary(summary: str, impact: str, action: str, redact: Callable[[str], str]) -> str:
    lines = []
    for index, (label, value) in enumerate((("판단", summary), ("영향", impact), ("대응", action)), 1):
        text = " ".join(redact(value).split())
        if len(text) > 240:
            text = text[:239] + "…"
        lines.append(f"{index}. {label}: {_markdown_text(text)}")
    return "\n".join(lines)
