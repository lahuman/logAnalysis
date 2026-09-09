"""Resolve Java stack frames against immutable Git objects."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Protocol

from log_analyzer.errors import LogAnalyzerError
from log_analyzer.models import ErrorEvent, ParsedError, SourceContext, StackFrame
from log_analyzer.parsers.java import normalize_java_class_name
from .java import enclosing_method_lines

_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_PACKAGE_RE = re.compile(
    r"package\s+(?P<package>[A-Za-z_$][A-Za-z0-9_.$]*)\s*;"
)


class ServiceSourceConfig(Protocol):
    repository: Path
    source_roots: Sequence[PurePosixPath]
    application_packages: Sequence[str]
    framework_packages: Sequence[str]


class SourceResolutionError(LogAnalyzerError):
    """Git or repository infrastructure could not be used safely."""


class GitSourceResolver:
    def __init__(
        self,
        services: Mapping[str, ServiceSourceConfig],
        *,
        context_lines: int = 75,
        max_file_bytes: int = 256 * 1024,
        max_change_context_chars: int = 30_000,
        git_timeout_seconds: float = 10.0,
    ) -> None:
        if context_lines < 0:
            raise ValueError("context_lines must not be negative")
        if max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")
        if max_change_context_chars < 1:
            raise ValueError("max_change_context_chars must be positive")
        if git_timeout_seconds <= 0:
            raise ValueError("git_timeout_seconds must be positive")
        self._services = dict(services)
        self._context_lines = context_lines
        self._max_file_bytes = max_file_bytes
        self._max_change_context_chars = max_change_context_chars
        self._git_timeout_seconds = git_timeout_seconds

    def healthcheck(self) -> None:
        for config in self._services.values():
            self._validate_repository(Path(config.repository))

    def resolve(
        self,
        event: ErrorEvent,
        parsed: ParsedError,
        *,
        resolved_commit_hint: str | None = None,
    ) -> SourceContext | None:
        config = self._services.get(event.service)
        if config is None:
            return None

        selected = _select_frame(
            parsed.frames,
            config.application_packages,
            config.framework_packages,
        )
        if selected is None or selected.class_name is None or selected.line_number is None:
            return None
        class_name = normalize_java_class_name(selected.class_name)
        if class_name is None:
            return None

        repository = self._validate_repository(Path(config.repository))
        revision_source = "event_commit"
        git_reference: str | None = None
        if event.git_commit:
            commit = event.git_commit.strip()
            if not _COMMIT_RE.fullmatch(commit):
                return None
            if not self._commit_exists(repository, commit):
                return None
            if not self._commit_is_reachable(repository, commit):
                return None
        elif resolved_commit_hint:
            commit = resolved_commit_hint.strip()
            if not _COMMIT_RE.fullmatch(commit):
                return None
            if not self._commit_exists(repository, commit):
                return None
            if not self._commit_is_reachable(repository, commit):
                return None
            revision_source = "repository_ref"
            git_reference = str(getattr(config, "branch", "HEAD"))
        else:
            git_reference = str(getattr(config, "branch", "HEAD")).strip() or "HEAD"
            commit = self._resolve_reference(repository, git_reference)
            if commit is None:
                return None
            revision_source = "repository_ref"

        class_path = PurePosixPath(*class_name.split(".")).with_suffix(".java")
        expected_package = class_name.rpartition(".")[0]
        for configured_root in config.source_roots:
            source_root = _safe_source_root(configured_root)
            candidate = source_root / class_path if source_root.parts else class_path
            source_path = candidate.as_posix()
            source = self._read_blob(repository, commit, source_path)
            if source is None:
                continue
            if not _package_matches(source, expected_package):
                continue

            lines = source.splitlines()
            if not (1 <= selected.line_number <= len(lines)):
                continue
            start = max(1, selected.line_number - self._context_lines)
            end = min(len(lines), selected.line_number + self._context_lines)
            method_lines = enclosing_method_lines(source, selected.line_number)
            if method_lines is not None:
                start = min(start, method_lines[0])
                end = max(end, method_lines[1])
            snippet = "\n".join(lines[start - 1 : end])
            change_context = ""
            if revision_source == "repository_ref":
                change_context = self._read_change_context(
                    repository,
                    commit,
                    source_path,
                    selected.line_number,
                    int(getattr(config, "diff_history", 5)),
                )
            return SourceContext(
                service=event.service,
                git_commit=commit,
                repository_path=repository,
                source_path=source_path,
                line_number=selected.line_number,
                function_name=selected.function_name,
                class_name=class_name,
                source_code=snippet,
                context_start_line=start,
                context_end_line=end,
                revision_source=revision_source,
                git_reference=git_reference,
                git_change_context=change_context,
            )
        return None

    def _resolve_reference(self, repository: Path, reference: str) -> str | None:
        result = self._git(
            repository,
            ("rev-parse", "--verify", f"{reference}^{{commit}}"),
            allow_failure=True,
        )
        if result.returncode != 0:
            return None
        commit = result.stdout.decode("ascii", errors="replace").strip()
        return commit if _COMMIT_RE.fullmatch(commit) else None

    def _read_change_context(
        self,
        repository: Path,
        commit: str,
        source_path: str,
        line_number: int,
        history_limit: int,
    ) -> str:
        history_limit = min(max(history_limit, 1), 50)
        sections: list[str] = []
        blame = self._git(
            repository,
            (
                "blame",
                "--line-porcelain",
                "-L",
                f"{line_number},{line_number}",
                commit,
                "--",
                source_path,
            ),
            allow_failure=True,
        )
        if blame.returncode == 0 and blame.stdout:
            sections.append(
                "## 오류 발생 줄의 Git blame\n"
                + blame.stdout.decode("utf-8", errors="replace").strip()
            )

        history = self._git(
            repository,
            (
                "log",
                "--no-color",
                "--no-ext-diff",
                f"--max-count={history_limit}",
                "--format=commit %H%nDate: %aI%nSubject: %s",
                "-p",
                "--unified=3",
                commit,
                "--",
                source_path,
            ),
            allow_failure=True,
        )
        if history.returncode == 0 and history.stdout:
            sections.append(
                "## 소스 파일의 최근 변경 이력\n"
                + history.stdout.decode("utf-8", errors="replace").strip()
            )
        value = "\n\n".join(sections)
        if len(value) <= self._max_change_context_chars:
            return value
        marker = "\n[TRUNCATED]"
        return value[: self._max_change_context_chars - len(marker)] + marker

    def _validate_repository(self, repository: Path) -> Path:
        try:
            resolved = repository.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise SourceResolutionError(f"repository is not accessible: {repository}") from exc
        if not resolved.is_dir():
            raise SourceResolutionError(f"repository is not a directory: {resolved}")
        result = self._git(
            resolved, ("rev-parse", "--is-bare-repository"), allow_failure=True
        )
        if result.returncode != 0:
            raise SourceResolutionError(f"not a Git repository: {resolved}")
        is_bare = result.stdout.decode("ascii", errors="replace").strip() == "true"
        if not is_bare:
            top_level = self._git(
                resolved, ("rev-parse", "--show-toplevel"), allow_failure=True
            )
            if top_level.returncode != 0:
                raise SourceResolutionError(f"not a Git repository: {resolved}")
            discovered = Path(
                top_level.stdout.decode("utf-8", errors="replace").strip()
            ).resolve()
            if discovered != resolved:
                raise SourceResolutionError(
                    f"repository path is not its Git root: {resolved}"
                )
        return resolved

    def _commit_exists(self, repository: Path, commit: str) -> bool:
        result = self._git(
            repository,
            ("cat-file", "-e", f"{commit}^{{commit}}"),
            allow_failure=True,
        )
        return result.returncode == 0

    def _commit_is_reachable(self, repository: Path, commit: str) -> bool:
        result = self._git(
            repository,
            (
                "for-each-ref",
                f"--contains={commit}",
                "--format=%(refname)",
                "refs/heads",
                "refs/remotes",
                "refs/tags",
            ),
            allow_failure=True,
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    def _read_blob(
        self, repository: Path, commit: str, source_path: str
    ) -> str | None:
        object_name = f"{commit}:{source_path}"
        size_result = self._git(
            repository, ("cat-file", "-s", object_name), allow_failure=True
        )
        if size_result.returncode != 0:
            return None
        try:
            size = int(size_result.stdout.decode("ascii").strip())
        except (UnicodeDecodeError, ValueError):
            raise SourceResolutionError("Git returned an invalid blob size")
        if size > self._max_file_bytes:
            return None

        show_result = self._git(
            repository, ("show", object_name), allow_failure=True
        )
        if show_result.returncode != 0 or len(show_result.stdout) > self._max_file_bytes:
            return None
        if b"\x00" in show_result.stdout:
            return None
        try:
            return show_result.stdout.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None

    def _git(
        self,
        repository: Path,
        arguments: Sequence[str],
        *,
        allow_failure: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        try:
            result = subprocess.run(
                ["git", "--no-optional-locks", "-C", str(repository), *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self._git_timeout_seconds,
                check=False,
                env=environment,
            )
        except FileNotFoundError as exc:
            raise SourceResolutionError("git executable was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise SourceResolutionError("git command timed out") from exc
        except OSError as exc:
            raise SourceResolutionError("git command could not be started") from exc
        if not allow_failure and result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace")[:500].strip()
            raise SourceResolutionError(detail or "git command failed")
        return result


def _select_frame(
    frames: Sequence[StackFrame],
    application_packages: Sequence[str],
    framework_packages: Sequence[str],
) -> StackFrame | None:
    applications = _clean_packages(application_packages)
    frameworks = _clean_packages(framework_packages)
    for frame in frames:
        if frame.class_name is None or frame.line_number is None:
            continue
        if _matches_package(frame.class_name, frameworks):
            continue
        if applications:
            if _matches_package(frame.class_name, applications):
                return frame
        elif frame.in_application:
            return frame
    return None


def _safe_source_root(value: PurePosixPath | str) -> PurePosixPath:
    raw = str(value).replace("\\", "/")
    root = PurePosixPath(raw)
    if root.is_absolute() or any(part == ".." or ":" in part for part in root.parts):
        raise SourceResolutionError(f"unsafe source root: {value}")
    return PurePosixPath(*[part for part in root.parts if part not in ("", ".")])


def _package_matches(source: str, expected: str) -> bool:
    position = 0
    while position < len(source):
        while position < len(source) and source[position].isspace():
            position += 1
        if source.startswith("//", position):
            newline = source.find("\n", position + 2)
            if newline < 0:
                position = len(source)
                break
            position = newline + 1
            continue
        if source.startswith("/*", position):
            end = source.find("*/", position + 2)
            if end < 0:
                return False
            position = end + 2
            continue
        break
    match = _PACKAGE_RE.match(source, position)
    if not expected:
        return match is None
    return bool(match and match.group("package") == expected)


def _clean_packages(packages: Sequence[str]) -> tuple[str, ...]:
    return tuple(package.strip().rstrip(".") for package in packages if package.strip())


def _matches_package(class_name: str, packages: Sequence[str]) -> bool:
    return any(
        class_name == package or class_name.startswith(f"{package}.")
        for package in packages
    )
