"""Canonical data models. Backend-agnostic. Everything flows through these."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class Note(BaseModel):
    """A single note. The canonical representation that all backends
    must produce and consume."""

    path: str = Field(
        ..., description="Unique path identifier, e.g. 'projects/companion.md'"
    )
    content: str = Field(default="", description="Markdown body")
    tags: list[str] = Field(default_factory=list, description="Organizational tags")
    created: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Immutable creation timestamp",
    )
    updated: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Last modification timestamp",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Extension fields for future use"
    )

    @property
    def title(self) -> str:
        """Derive title from first H1 heading, fall back to filename."""
        for line in self.content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("# ") and not stripped.startswith("## "):
                return stripped[2:].strip()
        stem = Path(self.path).stem
        return stem.replace("-", " ").replace("_", " ").title()

    @property
    def folder(self) -> str:
        """Extract folder from path. Empty string if root."""
        parts = Path(self.path).parts
        return str(Path(*parts[:-1])) if len(parts) > 1 else ""

    @property
    def size_tokens(self) -> int:
        """Rough token estimate (1 token ≈ 4 chars)."""
        return len(self.content) // 4


class NoteSummary(BaseModel):
    """Lightweight note info for list/search results. No content."""

    path: str
    title: str
    tags: list[str] = Field(default_factory=list)
    updated: datetime
    size_tokens: int = 0
    has_previous: bool = False

    @classmethod
    def from_note(cls, note: Note, has_previous: bool = False) -> "NoteSummary":
        return cls(
            path=note.path,
            title=note.title,
            tags=note.tags,
            updated=note.updated,
            size_tokens=note.size_tokens,
            has_previous=has_previous,
        )


class SearchResult(BaseModel):
    """A single search result with relevance context."""

    note: NoteSummary
    snippet: str = Field(default="", description="Content excerpt showing match")
    score: float = Field(default=0.0, description="Relevance score 0-1")
