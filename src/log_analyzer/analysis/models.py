"""Validated request and response models for incident analysis."""

from __future__ import annotations

import json
import posixpath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AnalysisRequest(_StrictModel):
    """The complete, bounded context sent to an incident analyzer."""

    service: str = Field(min_length=1, max_length=200)
    environment: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=200)
    fingerprint: str = Field(min_length=1, max_length=256)
    error_type: str = Field(min_length=1, max_length=500)
    message: str = Field(min_length=1, max_length=8_000)
    stack_trace: str = Field(max_length=30_000)
    parse_warnings: tuple[str, ...] = Field(default_factory=tuple, max_length=50)
    git_commit: str = Field(min_length=1, max_length=128)
    source_path: str = Field(min_length=1, max_length=2_000)
    line_number: int = Field(ge=1)
    function_name: str | None = Field(default=None, max_length=1_000)
    class_name: str | None = Field(default=None, max_length=1_000)
    source_code: str = Field(min_length=1, max_length=100_000)
    context_start_line: int = Field(ge=1)
    context_end_line: int = Field(ge=1)
    revision_source: Literal["event_commit", "repository_ref"] = "event_commit"
    git_reference: str | None = Field(default=None, max_length=512)
    git_change_context: str = Field(default="", max_length=30_000)

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or ":" in normalized.split("/", 1)[0]:
            raise ValueError("source_path must be repository-relative")
        normalized = posixpath.normpath(normalized)
        if normalized in {"", ".", ".."} or normalized.startswith("../"):
            raise ValueError("source_path must not traverse outside the repository")
        return normalized

    @model_validator(mode="after")
    def validate_line_range(self) -> "AnalysisRequest":
        if self.context_start_line > self.context_end_line:
            raise ValueError("context_start_line must not exceed context_end_line")
        if not self.context_start_line <= self.line_number <= self.context_end_line:
            raise ValueError("line_number must be inside the supplied source context")
        return self

    def to_prompt_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )


class Evidence(_StrictModel):
    file: str = Field(min_length=1, max_length=2_000)
    line: int = Field(ge=1)
    description: str = Field(min_length=1, max_length=4_000)


class RootCause(_StrictModel):
    cause: str = Field(min_length=1, max_length=8_000)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(max_length=50)


class RecommendedFix(_StrictModel):
    description: str = Field(min_length=1, max_length=8_000)
    files: list[str] = Field(max_length=50)
    risk: Literal["low", "medium", "high"]


class AnalysisResult(_StrictModel):
    summary: str = Field(min_length=1, max_length=8_000)
    root_causes: list[RootCause] = Field(max_length=20)
    recommended_fixes: list[RecommendedFix] = Field(max_length=20)
    validation_steps: list[str] = Field(max_length=50)
    unknowns: list[str] = Field(max_length=50)


def validated_evidence(result: AnalysisResult, request: AnalysisRequest) -> AnalysisResult:
    """Remove file and line claims that are outside the supplied source context."""

    expected_path = posixpath.normpath(request.source_path.replace("\\", "/"))

    def is_expected_path(value: str) -> bool:
        value = value.replace("\\", "/")
        if value.startswith("/") or ".." in value.split("/"):
            return False
        return posixpath.normpath(value) == expected_path

    causes: list[RootCause] = []
    for root_cause in result.root_causes:
        evidence = [
            item
            for item in root_cause.evidence
            if is_expected_path(item.file)
            and request.context_start_line <= item.line <= request.context_end_line
        ]
        causes.append(root_cause.model_copy(update={"evidence": evidence}))

    fixes: list[RecommendedFix] = []
    for fix in result.recommended_fixes:
        files = [
            path
            for path in fix.files
            if is_expected_path(path)
        ]
        fixes.append(fix.model_copy(update={"files": files}))

    return result.model_copy(
        update={"root_causes": causes, "recommended_fixes": fixes}
    )
