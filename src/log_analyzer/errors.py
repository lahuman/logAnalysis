"""Domain exceptions used across the application."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class LogAnalyzerError(Exception):
    """Base class for expected application failures."""


class ConfigError(LogAnalyzerError):
    """Raised when configuration cannot be loaded or validated."""

    def __init__(self, message: str, *, diagnostic_message: str | None = None) -> None:
        super().__init__(message)
        # The original message can contain a full Pydantic input or TOML value.
        self.diagnostic_message = diagnostic_message or (
            "Configuration is invalid; check configuration fields and required credentials."
        )


class StorageError(LogAnalyzerError):
    """Raised when persistent state cannot be read or written."""


class AlreadyRunningError(LogAnalyzerError):
    """Raised when another process owns the execution lock."""

    def __init__(self, metadata: Mapping[str, Any] | None = None) -> None:
        self.metadata = dict(metadata or {})
        owner = f" (owner: {self.metadata})" if self.metadata else ""
        super().__init__(f"another log-analyzer process is already running{owner}")


class UnsupportedPlatformError(LogAnalyzerError):
    """Raised when an operating-system safety primitive is unavailable."""
