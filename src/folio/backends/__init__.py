```python
"""Backend interface and factory."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from folio.config import FolioConfig
    from folio.models import Note, NoteSummary, SearchResult


class FolioBackend(ABC):
    """The contract every backend must fulfill. 10 methods, no more."""

    @abstractmethod
    def create(self, note: "Note") -> "Note": ...

    @abstractmethod
    def read(self, path: str, section: str | None = None) -> "Note": ...

    @abstractmethod
    def update(
        self,
        path: str,
        content: str | None,
        mode: str = "replace",
        target: str | None = None,
        tags: list[str] | None = None,
    ) -> "Note": ...

    @abstractmethod
    def delete(self, path: str) -> None: ...

    @abstractmethod
    def move(self, source: str, target: str) -> "Note": ...

    @abstractmethod
    def list(self, folder: str | None = None) -> list["NoteSummary"]: ...

    @abstractmethod
    def search(
        self,
        query: str,
        tags: list[str] | None = None,
        folder: str | None = None,
        sort: str = "relevance",
        updated_since: str | None = None,
        limit: int = 10,
    ) -> list["SearchResult"]: ...

    @abstractmethod
    def undo(self, path: str) -> "Note": ...

    @abstractmethod
    def export_all(self) -> list["Note"]: ...

    @abstractmethod
    def import_all(self, notes: list["Note"]) -> None: ...


def get_backend(config: "FolioConfig") -> FolioBackend:
    """Factory. Returns the right backend based on config."""
    match config.backend:
        case "local":
            from folio.backends.local import LocalBackend
            return LocalBackend(config.local)
        case "notion":
            from folio.backends.notion import NotionBackend
            return NotionBackend(config.notion)
        case _:
            raise ValueError(f"Unknown backend: {config.backend}")

```
