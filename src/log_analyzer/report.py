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

from .analysis.models import AnalysisRequest, AnalysisResult, validated_evidence
from .analysis.openai_responses import PROMPT_VERSION
from .analysis.redact import SecretRedactor


_FALLBACK_TEMPLATE = """# Java incident analysis report

- Status: ${status}
- Event: ${event_id}
- Service: ${service}
- Environment: ${environment}
- Version: ${version}
- Analyzed Git commit: ${git_commit}
- Source revision basis: ${revision_source}
- Git reference: ${git_reference}
- Occurrences: ${occurrence_count}
- First seen: ${first_seen}
- Last seen: ${last_seen}
- Analyzed at: ${analyzed_at}
- Model: ${model}
- Prompt version: ${prompt_version}

## Error

- Type: ${error_type}
- Message: ${message}

## Representative stack trace

```text
${stack_trace}
```

## Confirmed source location

${source_location}

## Git change context

${git_change_context}

## Summary

${summary}

## Evidence-based root causes

${root_causes}

## Recommended fixes

${recommended_fixes}

## Validation and regression tests

${validation_steps}

## Unknowns

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
            source_location=(
                f"`{_inline_code(safe.source_path)}:{safe.line_number}`"
                + (
                    f" (`{_inline_code(safe.function_name)}`)"
                    if safe.function_name
                    else ""
                )
            ),
            revision_source=_markdown_text(safe.revision_source),
            git_reference=_markdown_text(safe.git_reference or "-"),
            git_change_context=(
                "```text\n"
                + _fenced_text(redact(safe.git_change_context))
                + "\n```"
                if safe.git_change_context
                else "- Not collected because the deployment commit was supplied by the event."
            ),
            summary=_markdown_text(redact(result.summary)),
            root_causes=_root_causes(result, redact),
            recommended_fixes=_recommended_fixes(result, redact),
            validation_steps=_string_list(result.validation_steps, redact),
            unknowns=_string_list(result.unknowns, redact),
        )
        return self._atomic_write(
            event_id,
            self._template.substitute(values),
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
            source_location="No source was resolved; the LLM was not called.",
            revision_source="unavailable",
            git_reference="-",
            git_change_context="- No Git change context was available.",
            summary=_markdown_text(redact(reason)),
            root_causes="- Not analyzed because source evidence was unavailable.",
            recommended_fixes="- Resolve the deployment commit and source mapping, then retry.",
            validation_steps="- Verify the service Git repository, commit, and source roots.",
            unknowns="- Root cause is unknown until matching source evidence is available.",
        )
        return self._atomic_write(
            event_id,
            self._template.substitute(values),
            source_name=source_name,
        )

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
            "status": _markdown_text(status),
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


def _root_causes(result: AnalysisResult, redact: Callable[[str], str]) -> str:
    if not result.root_causes:
        return "- No evidence-based root cause was returned."
    sections: list[str] = []
    for index, cause in enumerate(result.root_causes, start=1):
        sections.append(
            f"{index}. {_markdown_text(redact(cause.cause))} "
            f"(confidence: {cause.confidence:.2f})"
        )
        for evidence in cause.evidence:
            sections.append(
                "   - Evidence: "
                f"`{_inline_code(redact(evidence.file))}:{evidence.line}` - "
                f"{_markdown_text(redact(evidence.description))}"
            )
        if not cause.evidence:
            sections.append("   - Evidence: none within the supplied source context")
    return "\n".join(sections)


def _recommended_fixes(
    result: AnalysisResult, redact: Callable[[str], str]
) -> str:
    if not result.recommended_fixes:
        return "- No fix was recommended."
    sections: list[str] = []
    for fix in result.recommended_fixes:
        files = (
            ", ".join(f"`{_inline_code(redact(path))}`" for path in fix.files)
            or "none"
        )
        sections.append(
            f"- {_markdown_text(redact(fix.description))} "
            f"(risk: {fix.risk}; files: {files})"
        )
    return "\n".join(sections)


def _string_list(values: list[str], redact: Callable[[str], str]) -> str:
    if not values:
        return "- None."
    return "\n".join(f"- {_markdown_text(redact(value))}" for value in values)
