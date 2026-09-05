"""Parser for Java/JVM exception stack traces."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from collections.abc import Sequence

from log_analyzer.models import ErrorEvent, ParsedError, StackFrame

_DEFAULT_FRAMEWORK_PACKAGES = (
    "java",
    "javax",
    "jakarta",
    "jdk",
    "sun",
    "com.sun",
)
_JAVA_HINTS = ("java", "jvm", "openjdk", "hotspot")
_EXCEPTION_CLASS = (
    r"(?:[A-Za-z_$][A-Za-z0-9_$]*\.)*"
    r"[A-Za-z_$][A-Za-z0-9_$]*(?:Exception|Error|Throwable|Failure)"
)
_ROOT_HEADER_RE = re.compile(
    rf"(?P<type>{_EXCEPTION_CLASS})(?::\s*(?P<message>.*))?$"
)
_EXPLICIT_HEADER_RE = re.compile(
    r"(?P<type>(?:[A-Za-z_$][A-Za-z0-9_$]*\.)*"
    r"[A-Za-z_$][A-Za-z0-9_$]*)(?::\s*(?P<message>.*))?$"
)
_THREAD_PREFIX_RE = re.compile(r'^Exception in thread\s+"[^"]+"\s+')
_FRAME_RE = re.compile(r"^at\s+(?P<call>[^\s(]+)\((?P<location>[^)]*)\)$")
_OMITTED_RE = re.compile(r"^\.\.\.\s+(?P<count>[0-9]+)\s+more$")


@dataclass
class _ExceptionSection:
    error_type: str | None
    message: str
    frames: list[StackFrame] = field(default_factory=list)
    omitted: int = 0


class JavaErrorParser:
    name = "java"

    def __init__(
        self,
        application_packages: Sequence[str] = (),
        framework_packages: Sequence[str] = (),
        *,
        max_input_chars: int = 1_000_000,
    ) -> None:
        if max_input_chars < 1:
            raise ValueError("max_input_chars must be positive")
        self._application_packages = _clean_packages(application_packages)
        self._framework_packages = _clean_packages(
            (*_DEFAULT_FRAMEWORK_PACKAGES, *framework_packages)
        )
        self._max_input_chars = max_input_chars

    def can_parse(self, event: ErrorEvent) -> bool:
        hints = (event.language_hint or "", event.runtime_hint or "")
        if any(hint and any(token in hint.lower() for token in _JAVA_HINTS) for hint in hints):
            return True
        text = _event_text(event)
        if "Caused by:" in text or "Suppressed:" in text:
            return True
        for line in text.splitlines():
            stripped = line.strip()
            if _FRAME_RE.match(stripped):
                return True
            if _parse_header(stripped, explicit=False):
                return True
        return False

    def parse(self, event: ErrorEvent) -> ParsedError:
        text = _event_text(event)
        warnings: list[str] = []
        if len(text) > self._max_input_chars:
            text = text[: self._max_input_chars]
            warnings.append("stack_trace_truncated")

        sections: list[_ExceptionSection] = []
        current: _ExceptionSection | None = None
        suppressed_indentation: int | None = None
        suppressed_count = 0

        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            indentation = len(raw_line) - len(raw_line.lstrip(" \t"))

            if stripped.startswith("Suppressed:"):
                if _parse_header(stripped[len("Suppressed:") :].strip(), explicit=True):
                    suppressed_count += 1
                suppressed_indentation = indentation
                continue

            if stripped.startswith("Caused by:"):
                header = _parse_header(
                    stripped[len("Caused by:") :].strip(), explicit=True
                )
                if (
                    suppressed_indentation is not None
                    and indentation >= suppressed_indentation
                ):
                    continue
                suppressed_indentation = None
                if header:
                    current = _ExceptionSection(*header)
                    sections.append(current)
                else:
                    warnings.append("invalid_cause_header")
                continue

            if suppressed_indentation is not None:
                continue

            if not sections:
                header = _parse_header(stripped, explicit=False)
                if header:
                    current = _ExceptionSection(*header)
                    sections.append(current)
                    continue

            omitted_match = _OMITTED_RE.match(stripped)
            if omitted_match:
                if current is None:
                    warnings.append("orphan_omitted_frames")
                else:
                    current.omitted = int(omitted_match.group("count"))
                continue

            frame = self._parse_frame(stripped)
            if frame is None:
                continue
            if current is None:
                current = _ExceptionSection(
                    event.error_type,
                    (event.message or "").strip(),
                )
                sections.append(current)
            current.frames.append(frame)

        if suppressed_count:
            warnings.append(f"suppressed_exceptions_ignored:{suppressed_count}")

        if not sections:
            error_type = event.error_type.strip() if event.error_type else None
            message = (event.message or "").strip()
            warnings.extend(("no_exception_header", "no_stack_frames"))
            return ParsedError(
                language="java",
                error_type=error_type,
                message=message,
                frames=(),
                cause_chain=(
                    (_format_exception(error_type, message),) if error_type else ()
                ),
                parser_name=self.name,
                parse_warnings=tuple(dict.fromkeys(warnings)),
            )

        reconstructed: list[list[StackFrame]] = []
        for section in sections:
            frames = list(section.frames)
            if section.omitted:
                if not reconstructed:
                    warnings.append("omitted_frames_without_parent")
                else:
                    parent_frames = reconstructed[-1]
                    if section.omitted > len(parent_frames):
                        frames.extend(parent_frames)
                        warnings.append("omitted_frame_count_exceeds_parent")
                    else:
                        frames.extend(parent_frames[-section.omitted :])
            reconstructed.append(frames)

        deepest = sections[-1]
        frames = tuple(reconstructed[-1])
        if not frames:
            warnings.append("no_stack_frames")

        cause_chain = tuple(
            _format_exception(section.error_type, section.message)
            for section in sections
            if section.error_type
        )
        return ParsedError(
            language="java",
            error_type=deepest.error_type,
            message=deepest.message or (event.message or "").strip(),
            frames=frames,
            cause_chain=cause_chain,
            parser_name=self.name,
            parse_warnings=tuple(dict.fromkeys(warnings)),
        )

    def _parse_frame(self, line: str) -> StackFrame | None:
        match = _FRAME_RE.match(line)
        if not match:
            return None

        call = match.group("call")
        module_name: str | None = None
        if "/" in call:
            module_part, call = call.rsplit("/", 1)
            module_part = module_part.rstrip("/")
            if module_part:
                module_name = module_part.split("@", 1)[0]
        if "." not in call:
            return None
        class_name, function_name = call.rsplit(".", 1)
        if not _valid_runtime_class(class_name) or not function_name:
            return None

        location = match.group("location").strip()
        file_path: str | None = None
        line_number: int | None = None
        if location not in {"Native Method", "Unknown Source"}:
            candidate_file, separator, candidate_line = location.rpartition(":")
            if separator and candidate_line.isdigit():
                file_path = candidate_file or None
                parsed_line = int(candidate_line)
                line_number = parsed_line if parsed_line > 0 else None
            elif location:
                file_path = location

        return StackFrame(
            file_path=file_path,
            line_number=line_number,
            column_number=None,
            function_name=function_name,
            class_name=class_name,
            module_name=module_name,
            in_application=self._is_application_class(class_name),
        )

    def _is_application_class(self, class_name: str) -> bool:
        if _matches_package(class_name, self._framework_packages):
            return False
        if self._application_packages:
            return _matches_package(class_name, self._application_packages)
        return True


def normalize_java_class_name(class_name: str) -> str | None:
    """Return the top-level class used to derive a Java source path."""

    normalized = class_name.split("$", 1)[0]
    if not normalized or not _valid_runtime_class(normalized):
        return None
    return normalized


def _event_text(event: ErrorEvent) -> str:
    return event.stack_trace or event.message or ""


def _parse_header(line: str, *, explicit: bool) -> tuple[str, str] | None:
    candidate = _THREAD_PREFIX_RE.sub("", line, count=1).strip()
    match = (_EXPLICIT_HEADER_RE.fullmatch(candidate) if explicit else _ROOT_HEADER_RE.search(candidate))
    if not match:
        return None
    return match.group("type"), (match.group("message") or "").strip()


def _format_exception(error_type: str | None, message: str) -> str:
    if not error_type:
        return message
    return f"{error_type}: {message}" if message else error_type


def _valid_runtime_class(class_name: str) -> bool:
    if "/" in class_name or "\\" in class_name or ".." in class_name:
        return False
    return all(
        bool(part) and re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", part) is not None
        for part in class_name.split(".")
    )


def _clean_packages(packages: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(package.strip().rstrip(".") for package in packages if package.strip()))


def _matches_package(class_name: str, packages: Sequence[str]) -> bool:
    return any(
        class_name == package or class_name.startswith(f"{package}.")
        for package in packages
    )
