"""Canonical data models. Backend-agnostic. Everything flows through these."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
import re


class Note(BaseModel):
    """A single note. The canonical representation that all backends
    must produce and consume."""

    path: str = Field(
        ..., description="Unique path identifier, e.g. 'projects/companion.md'"
    )
    title: str = Field(
        default="", description="Display name of the note"
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

    def __init__(self, **data):
        if "content" in data and isinstance(data["content"], str):
            data["content"] = data["content"].replace("\\n", "\n")

        # Fallback for title if empty
        if not data.get("title"):
            path_str = data.get("path", "")
            if path_str:
                stem = Path(path_str).stem
                data["title"] = stem.replace("-", " ").replace("_", " ").title()
            else:
                data["title"] = "Untitled"

        super().__init__(**data)

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


def slugify_title(title: str) -> str | None:
    """Convert a page title to a Folio-style path slug.
    Shared logic used by SyncEngine and Move actions to evaluate auto-linking."""
    slug = title.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)  # Remove special chars
    slug = re.sub(r"[\s_]+", "-", slug)  # Spaces/underscores → hyphens
    slug = re.sub(r"-+", "-", slug).strip("-")  # Collapse hyphens
    slug = slug[:500]  # Truncate to avoid path length issues
    slug = slug.strip("-")
    return f"{slug}.md" if slug else None


def evaluate_move_title(old_path: str, old_title: str, new_path: str) -> str:
    """Determine the correct title after a move operation.
    If the old title exactly matched the old path (Auto-Linked), derive a new title.
    If it didn't match (Explicit), preserve the old title."""
    old_slug = old_path.split("/")[-1]
    expected_old_slug = slugify_title(old_title) if old_title and old_title != "Untitled" else None

    is_untitled = old_slug.startswith("untitled-")
    was_auto_linked = bool(expected_old_slug and old_slug == expected_old_slug)

    if was_auto_linked or is_untitled:
        # Auto-linked: Derive new title from the new path
        new_stem = Path(new_path).stem
        return new_stem.replace("-", " ").replace("_", " ").title()
    else:
        # Explicit: Preserve the old title
        return old_title
