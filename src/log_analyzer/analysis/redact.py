"""Deterministic secret removal before data leaves the process."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..errors import LogAnalyzerError
from .models import AnalysisRequest, AnalysisResult


class RedactionError(LogAnalyzerError):
    """Raised when safe analysis input cannot be produced."""


@dataclass(frozen=True)
class RedactionLimits:
    message_chars: int = 4_000
    stack_trace_chars: int = 20_000
    source_code_chars: int = 60_000
    git_change_chars: int = 30_000
    warning_chars: int = 500
    warning_count: int = 20


_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
            r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (
        re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:bearer|basic)\s+[^\s,;]+"),
        r"\1[REDACTED_AUTHORIZATION]",
    ),
    (
        re.compile(r"(?i)\b(cookie|set-cookie)\s*:\s*[^\r\n]+"),
        r"\1: [REDACTED_COOKIE]",
    ),
    (
        re.compile(
            r"(?i)([\"']?(?:password|passwd|pwd|secret|api[_-]?key|"
            r"access[_-]?token|refresh[_-]?token|session[_-]?id)[\"']?"
            r"\s*[:=]\s*)"
            r"(?:[\"'][^\"'\r\n]*[\"']|[^\s,;}]+)"
        ),
        r"\1[REDACTED_SECRET]",
    ),
    (
        re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
        "[REDACTED_API_KEY]",
    ),
    (
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
        "[REDACTED_ACCESS_KEY]",
    ),
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\."
            r"[A-Za-z0-9_-]{5,}\b"
        ),
        "[REDACTED_JWT]",
    ),
    (
        re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
        "[REDACTED_EMAIL]",
    ),
    (
        re.compile(r"(?<!\d)\d{6}-[1-4]\d{6}(?!\d)"),
        "[REDACTED_NATIONAL_ID]",
    ),
    (
        re.compile(r"(?<!\d)(?:\+?82[- ]?)?01[016789][- ]?\d{3,4}[- ]?\d{4}(?!\d)"),
        "[REDACTED_PHONE]",
    ),
    (
        re.compile(
            r"(?i)\b((?:customer|account|user)[_-]?id\s*[:=]\s*)"
            r"[^\s,;}]+"
        ),
        r"\1[REDACTED_IDENTIFIER]",
    ),
    (
        re.compile(r"(?i)\b((?:jdbc:[a-z0-9]+|https?)://)[^/\s:@]+:[^/\s@]+@"),
        r"\1[REDACTED_USERINFO]@",
    ),
    (
        re.compile(
            r"(?<![\w.])(?:25[0-5]|2[0-4]\d|1?\d?\d)"
            r"(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\w.])"
        ),
        "[REDACTED_IP]",
    ),
)


def _truncate(value: str, maximum: int) -> str:
    if maximum < 32:
        raise RedactionError("redaction limits must be at least 32 characters")
    if len(value) <= maximum:
        return value
    marker = "\n[TRUNCATED]"
    return value[: maximum - len(marker)] + marker


class SecretRedactor:
    def __init__(self, limits: RedactionLimits | None = None) -> None:
        self._limits = limits or RedactionLimits()

    def redact_text(self, value: str) -> str:
        if not isinstance(value, str):
            raise RedactionError("only text values can be redacted")
        try:
            for pattern, replacement in _REPLACEMENTS:
                value = pattern.sub(replacement, value)
        except (re.error, MemoryError) as exc:
            raise RedactionError("failed to redact analysis input") from exc
        return value

    def redact_request(self, request: AnalysisRequest) -> AnalysisRequest:
        """Return a redacted and size-bounded copy safe for serialization."""

        limits = self._limits
        warnings = tuple(
            _truncate(self.redact_text(value), limits.warning_chars)
            for value in request.parse_warnings[: limits.warning_count]
        )
        data = request.model_dump()
        data.update(
            service=self.redact_text(request.service),
            environment=self.redact_text(request.environment),
            version=self.redact_text(request.version),
            error_type=self.redact_text(request.error_type),
            message=_truncate(
                self.redact_text(request.message), limits.message_chars
            ),
            stack_trace=_truncate(
                self.redact_text(request.stack_trace), limits.stack_trace_chars
            ),
            parse_warnings=warnings,
            function_name=(
                self.redact_text(request.function_name)
                if request.function_name is not None
                else None
            ),
            class_name=(
                self.redact_text(request.class_name)
                if request.class_name is not None
                else None
            ),
            source_code=_truncate(
                self.redact_text(request.source_code), limits.source_code_chars
            ),
            git_reference=(
                self.redact_text(request.git_reference)
                if request.git_reference is not None
                else None
            ),
            git_change_context=_truncate(
                self.redact_text(request.git_change_context), limits.git_change_chars
            ) if request.git_change_context else "",
        )
        try:
            return AnalysisRequest.model_validate(data)
        except Exception as exc:
            raise RedactionError("redacted analysis input is invalid") from exc

    def redact_result(self, result: AnalysisResult) -> AnalysisResult:
        """Redact every model-controlled string before persistence."""

        def redact_value(value: object) -> object:
            if isinstance(value, str):
                return self.redact_text(value)
            if isinstance(value, list):
                return [redact_value(item) for item in value]
            if isinstance(value, dict):
                return {key: redact_value(item) for key, item in value.items()}
            return value

        try:
            return AnalysisResult.model_validate(redact_value(result.model_dump()))
        except Exception as exc:
            raise RedactionError("redacted analysis result is invalid") from exc
