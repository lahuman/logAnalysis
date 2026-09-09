from __future__ import annotations

import subprocess
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
import unittest

from log_analyzer.models import ErrorEvent, ParsedError, StackFrame
from log_analyzer.parsers import JavaErrorParser
from log_analyzer.source_code import GitSourceResolver, SourceResolutionError


JAVA_SOURCE = """/* package com.example.wrong; */ package com.example.order;

public class OrderService {
    public void save() {
        throw new IllegalStateException("old committed source");
    }
}
"""


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def event(commit: str | None, *, service: str = "order-api") -> ErrorEvent:
    return ErrorEvent(
        source_name="test",
        event_id="event-1",
        occurred_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        service=service,
        severity="ERROR",
        message="failed",
        git_commit=commit,
    )


def parsed(
    *,
    class_name: str = "com.example.order.OrderService$Worker",
    line_number: int | None = 5,
    in_application: bool = True,
) -> ParsedError:
    return ParsedError(
        language="java",
        error_type="java.lang.IllegalStateException",
        message="old committed source",
        frames=(
            StackFrame(
                file_path="OrderService.java",
                line_number=line_number,
                function_name="save",
                class_name=class_name,
                in_application=in_application,
            ),
        ),
        parser_name="java",
    )


class GitSourceResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary_directory.name)
        git(self.repository, "init")
        git(self.repository, "config", "user.email", "test@example.invalid")
        git(self.repository, "config", "user.name", "Test User")
        self.relative_source = Path(
            "module/src/main/java/com/example/order/OrderService.java"
        )
        source = self.repository / self.relative_source
        source.parent.mkdir(parents=True)
        source.write_text(JAVA_SOURCE, encoding="utf-8")
        git(self.repository, "add", self.relative_source.as_posix())
        git(self.repository, "commit", "-m", "initial")
        self.commit = git(self.repository, "rev-parse", "HEAD")
        self.config = SimpleNamespace(
            name="order-api",
            repository=self.repository,
            source_roots=(PurePosixPath("module/src/main/java"),),
            application_packages=("com.example.order",),
            framework_packages=("java", "jdk", "sun", "org.springframework"),
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def resolver(self, **overrides: object) -> GitSourceResolver:
        options: dict[str, object] = {
            "context_lines": 1,
            "max_file_bytes": 256 * 1024,
        }
        options.update(overrides)
        return GitSourceResolver(
            {"order-api": self.config},
            **options,  # type: ignore[arg-type]
        )

    def test_resolves_top_level_source_from_inner_class_at_exact_commit(self) -> None:
        source = self.repository / self.relative_source
        source.write_text(
            JAVA_SOURCE.replace("old committed source", "uncommitted source"),
            encoding="utf-8",
        )
        head_before = git(self.repository, "rev-parse", "HEAD")

        context = self.resolver().resolve(event(self.commit), parsed())

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(self.commit, context.git_commit)
        self.assertEqual(
            self.relative_source.as_posix(), context.source_path
        )
        self.assertEqual("com.example.order.OrderService", context.class_name)
        self.assertEqual(5, context.line_number)
        self.assertEqual(4, context.context_start_line)
        self.assertEqual(6, context.context_end_line)
        self.assertIn("old committed source", context.source_code)
        self.assertNotIn("uncommitted source", context.source_code)
        self.assertEqual(head_before, git(self.repository, "rev-parse", "HEAD"))

    def test_resolves_deepest_cause_instead_of_outer_exception(self) -> None:
        item = replace(event(self.commit), stack_trace=(
            "java.lang.RuntimeException: outer\n"
            "\tat com.example.order.Controller.call(Controller.java:10)\n"
            "Caused by: java.lang.IllegalStateException: old committed source\n"
            "\tat com.example.order.OrderService.save(OrderService.java:5)\n"
        ))
        selected = JavaErrorParser(("com.example.order",)).parse(item)
        resolved = self.resolver().resolve(item, selected)

        self.assertIsNotNone(resolved)
        self.assertEqual(self.relative_source.as_posix(), resolved.source_path)
        self.assertEqual(5, resolved.line_number)

    def test_includes_entire_long_method_beyond_context_window(self) -> None:
        method = (
            "    @Deprecated\n"
            "    public void save(\n"
            "        String value\n"
            "    ) throws IllegalStateException {\n"
            + "        value = value.trim();\n" * 100
            + '        throw new IllegalStateException("failed");\n'
            + "        // cleanup } is part of this method\n" * 100
            + "    }"
        )
        source = "package com.example.order;\npublic class OrderService {\n" + method + "\n}\n"
        (self.repository / self.relative_source).write_text(source, encoding="utf-8")
        git(self.repository, "add", self.relative_source.as_posix())
        git(self.repository, "commit", "-m", "long method")
        commit = git(self.repository, "rev-parse", "HEAD")
        failure_line = next(i for i, line in enumerate(source.splitlines(), 1) if "throw new" in line)

        resolved = self.resolver().resolve(event(commit), parsed(line_number=failure_line))

        self.assertIsNotNone(resolved)
        self.assertEqual(method, resolved.source_code)
        self.assertEqual(3, resolved.context_start_line)
        self.assertEqual(len(source.splitlines()) - 1, resolved.context_end_line)
        self.assertEqual(failure_line, resolved.line_number)

    def test_missing_event_commit_uses_repository_head_and_collects_recent_changes(self) -> None:
        source = self.repository / self.relative_source
        source.write_text(
            JAVA_SOURCE.replace("old committed source", "current branch source"),
            encoding="utf-8",
        )
        git(self.repository, "add", self.relative_source.as_posix())
        git(self.repository, "commit", "-m", "change failing operation")
        head = git(self.repository, "rev-parse", "HEAD")

        context = self.resolver().resolve(event(None), parsed())

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(head, context.git_commit)
        self.assertEqual("repository_ref", context.revision_source)
        self.assertEqual("HEAD", context.git_reference)
        self.assertIn("current branch source", context.source_code)
        self.assertIn("오류 발생 줄의 Git blame", context.git_change_context)
        self.assertIn("change failing operation", context.git_change_context)

    def test_resolved_commit_hint_keeps_fallback_revision_stable(self) -> None:
        original = self.commit
        source = self.repository / self.relative_source
        source.write_text(
            JAVA_SOURCE.replace("old committed source", "newer source"),
            encoding="utf-8",
        )
        git(self.repository, "add", self.relative_source.as_posix())
        git(self.repository, "commit", "-m", "newer commit")

        context = self.resolver().resolve(
            event(None), parsed(), resolved_commit_hint=original
        )

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(original, context.git_commit)
        self.assertEqual("repository_ref", context.revision_source)
        self.assertIn("old committed source", context.source_code)
        self.assertNotIn("newer source", context.source_code)

    def test_returns_none_for_unknown_service_missing_or_unsafe_commit(self) -> None:
        resolver = self.resolver()

        self.assertIsNone(
            resolver.resolve(event(self.commit, service="unknown"), parsed())
        )
        self.assertIsNone(resolver.resolve(event("deadbee"), parsed()))
        self.assertIsNone(resolver.resolve(event("--help"), parsed()))

    def test_rejects_commit_not_reachable_from_operator_managed_refs(self) -> None:
        source = self.repository / self.relative_source
        source.write_text(
            JAVA_SOURCE.replace("old committed source", "dangling source"),
            encoding="utf-8",
        )
        git(self.repository, "add", self.relative_source.as_posix())
        git(self.repository, "commit", "-m", "temporary dangling commit")
        dangling = git(self.repository, "rev-parse", "HEAD")
        git(self.repository, "reset", "--hard", "HEAD~1")

        self.assertIsNone(self.resolver().resolve(event(dangling), parsed()))

    def test_returns_none_when_package_declaration_does_not_match(self) -> None:
        source = self.repository / self.relative_source
        source.write_text(
            JAVA_SOURCE.replace(
                "package com.example.order;", "package com.example.other;"
            ),
            encoding="utf-8",
        )
        git(self.repository, "add", self.relative_source.as_posix())
        git(self.repository, "commit", "-m", "wrong package")
        wrong_package_commit = git(self.repository, "rev-parse", "HEAD")

        self.assertIsNone(
            self.resolver().resolve(event(wrong_package_commit), parsed())
        )

    def test_returns_none_for_oversized_source_or_unknown_line(self) -> None:
        self.assertIsNone(
            self.resolver(max_file_bytes=10).resolve(event(self.commit), parsed())
        )
        self.assertIsNone(
            self.resolver().resolve(
                event(self.commit), parsed(line_number=None)
            )
        )

    def test_recomputes_application_membership_from_service_packages(self) -> None:
        context = self.resolver().resolve(
            event(self.commit), parsed(in_application=False)
        )
        self.assertIsNotNone(context)

        other = parsed(class_name="org.vendor.OrderService", in_application=True)
        self.assertIsNone(self.resolver().resolve(event(self.commit), other))

    def test_rejects_source_root_traversal_before_git_show(self) -> None:
        unsafe = SimpleNamespace(
            repository=self.repository,
            source_roots=(PurePosixPath("../src/main/java"),),
            application_packages=("com.example.order",),
            framework_packages=(),
        )
        resolver = GitSourceResolver({"order-api": unsafe})

        with self.assertRaises(SourceResolutionError):
            resolver.resolve(event(self.commit), parsed())

    def test_healthcheck_rejects_non_git_directory(self) -> None:
        empty = self.repository / "not-git"
        empty.mkdir()
        bad_config = SimpleNamespace(
            repository=empty,
            source_roots=(PurePosixPath("src/main/java"),),
            application_packages=("com.example.order",),
            framework_packages=(),
        )

        with self.assertRaises(SourceResolutionError):
            GitSourceResolver({"order-api": bad_config}).healthcheck()


if __name__ == "__main__":
    unittest.main()
