"""Storage-neutral domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ErrorQuery:
    started_at: datetime
    ended_at: datetime
    severities: tuple[str, ...] = ()
    limit: int = 500

    def __post_init__(self) -> None:
        if self.started_at >= self.ended_at:
            raise ValueError("started_at must be earlier than ended_at")
        if self.limit <= 0:
            raise ValueError("limit must be greater than zero")


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    source_name: str
    event_id: str
    occurred_at: datetime
    service: str
    severity: str
    message: str
    environment: str | None = None
    version: str | None = None
    git_commit: str | None = None
    error_type: str | None = None
    stack_trace: str | None = None
    raw_log: str | None = None
    language_hint: str | None = None
    runtime_hint: str | None = None
    trace_id: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        for field_name in ("source_name", "event_id", "service", "severity"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be empty")
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))


@dataclass(frozen=True, slots=True)
class ErrorPage:
    events: tuple[ErrorEvent, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))


@dataclass(frozen=True, slots=True)
class StackFrame:
    file_path: str | None = None
    line_number: int | None = None
    column_number: int | None = None
    function_name: str | None = None
    class_name: str | None = None
    module_name: str | None = None
    in_application: bool = False


@dataclass(frozen=True, slots=True)
class ParsedError:
    language: str | None
    error_type: str | None
    message: str
    frames: tuple[StackFrame, ...] = ()
    cause_chain: tuple[str, ...] = ()
    parser_name: str = "generic"
    parse_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "frames", tuple(self.frames))
        object.__setattr__(self, "cause_chain", tuple(self.cause_chain))
        object.__setattr__(self, "parse_warnings", tuple(self.parse_warnings))


@dataclass(frozen=True, slots=True)
class SourceContext:
    service: str
    git_commit: str
    repository_path: Path
    source_path: str
    line_number: int
    function_name: str | None
    class_name: str
    source_code: str
    context_start_line: int
    context_end_line: int
    revision_source: str = "event_commit"
    git_reference: str | None = None
    git_change_context: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "repository_path", Path(self.repository_path))
        if self.line_number <= 0:
            raise ValueError("line_number must be greater than zero")
        if not (1 <= self.context_start_line <= self.line_number <= self.context_end_line):
            raise ValueError("source context line range does not contain line_number")
        if self.revision_source not in {"event_commit", "repository_ref"}:
            raise ValueError("revision_source must be event_commit or repository_ref")
