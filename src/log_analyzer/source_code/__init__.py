"""Read-only source code resolution."""

from .git import GitSourceResolver, SourceResolutionError

__all__ = ["GitSourceResolver", "SourceResolutionError"]
