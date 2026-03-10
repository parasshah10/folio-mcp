"""Local backend — markdown files on disk with git versioning."""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, List

import frontmatter
from git import Repo, InvalidGitRepositoryError, GitCommandError

from folio.backends import FolioBackend
from folio.config import LocalConfig
from folio.models import Note, NoteSummary, SearchResult
from folio.sections import extract_section, replace_section


# ---------------------------------------------------------------------------
# Search index entry (in-memory, rebuilt on startup)
# ---------------------------------------------------------------------------

class _IndexEntry:
    __slots__ = ("path", "title", "tags", "updated", "size_tokens", "content_lower")

    def __init__(self, note: Note):
        self.path = note.path
        self.title = note.title
        self.tags = note.tags
        self.updated = note.updated
        self.size_tokens = note.size_tokens
        self.content_lower = note.content.lower()


# ---------------------------------------------------------------------------
# Local Backend
# ---------------------------------------------------------------------------

class LocalBackend(FolioBackend):

    def __init__(self, config: LocalConfig):
        self.root = Path(config.root).expanduser().resolve()
        self.git_enabled = config.git
        self.git_remote = config.git_remote
        self.git_auto_push = config.git_auto_push
        self._repo: Repo | None = None
        self._index: dict[str, _IndexEntry] = {}

        self._ensure_root()
        if self.git_enabled:
            self._init_git()
        self._build_index()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _init_git(self) -> None:
        try:
            self._repo = Repo(self.root)
        except InvalidGitRepositoryError:
            self._repo = Repo.init(self.root)
            # Initial commit so HEAD exists
            gitignore = self.root / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text(".folio/\n")
            self._repo.index.add([".gitignore"])
            self._repo.index.commit("folio: init")

    def _build_index(self) -> None:
        """Scan all markdown files and build in-memory search index."""
        self._index.clear()
        for filepath in self.root.rglob("*.md"):
            rel = str(filepath.relative_to(self.root))
            try:
                note = self._read_file(rel)
                self._index[rel] = _IndexEntry(note)
            except Exception:
                continue  # skip malformed files

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        """Convert a note path to an absolute filesystem path. Validates safety."""
        # Normalize and block traversal
        clean = Path(path)
        if clean.is_absolute():
            raise PermissionError(f"Absolute paths not allowed: {path}")
        resolved = (self.root / clean).resolve()
        if not str(resolved).startswith(str(self.root)):
            raise PermissionError(f"Path traversal not allowed: {path}")
        return resolved

    def _relative(self, absolute: Path) -> str:
        """Convert absolute path back to note path."""
        return str(absolute.relative_to(self.root))

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def _read_file(self, path: str) -> Note:
        """Read a markdown file with frontmatter into a Note."""
        filepath = self._resolve(path)
        if not filepath.exists():
            raise FileNotFoundError(f"Note not found: {path}")

        post = frontmatter.load(str(filepath))

        return Note(
            path=path,
            title=post.metadata.get("title", ""),
            content=post.content,
            tags=post.metadata.get("tags", []),
            created=_parse_dt(post.metadata.get("created")),
            updated=_parse_dt(post.metadata.get("updated")),
            metadata={
                k: v for k, v in post.metadata.items()
                if k not in ("title", "tags", "created", "updated")
            },
        )

    def _write_file(self, note: Note) -> None:
        """Write a Note to disk as markdown with YAML frontmatter."""
        filepath = self._resolve(note.path)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        post = frontmatter.Post(note.content)
        post.metadata["title"] = note.title
        post.metadata["tags"] = note.tags
        post.metadata["created"] = note.created.isoformat()
        post.metadata["updated"] = note.updated.isoformat()
        for k, v in note.metadata.items():
            if k != "title":
                post.metadata[k] = v

        with open(filepath, "wb") as f:
            frontmatter.dump(post, f)

        # Update search index
        self._index[note.path] = _IndexEntry(note)

    def _has_previous(self, path: str) -> bool:
        """Check if git has a previous version of this file."""
        if not self._repo or not self.git_enabled:
            return False
        try:
            rel = str(Path(path))
            commits = list(self._repo.iter_commits(paths=rel, max_count=2))
            return len(commits) >= 2
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Git helpers
    # ------------------------------------------------------------------

    def _git_commit(self, message: str, paths: List[str] | None = None) -> None:
        """Stage and commit. No-op if git is disabled."""
        if not self._repo or not self.git_enabled:
            return
        try:
            if paths:
                for p in paths:
                    abs_path = self._resolve(p)
                    if abs_path.exists():
                        self._repo.index.add([str(abs_path.relative_to(self.root))])
                    else:
                        # File was deleted — stage the removal
                        try:
                            self._repo.index.remove([str(abs_path.relative_to(self.root))])
                        except Exception:
                            self._repo.git.add(A=True)
            else:
                self._repo.git.add(A=True)

            self._repo.index.commit(message)

            if self.git_auto_push and self.git_remote:
                self._repo.remote("origin").push()
        except Exception:
            pass  # git errors should never break note operations

    # ------------------------------------------------------------------
    # Backend methods: CRUD
    # ------------------------------------------------------------------

    def create(self, note: Note) -> Note:
        filepath = self._resolve(note.path)
        if filepath.exists():
            raise FileExistsError(f"Note already exists: {note.path}")

        now = datetime.now(timezone.utc)
        note = note.model_copy(update={"created": now, "updated": now})
        self._write_file(note)
        self._git_commit(f"folio: create {note.path}", [note.path])
        return note

    def read(self, path: str, section: str | None = None) -> Note:
        note = self._read_file(path)
        if section:
            section_content = extract_section(note.content, section)
            if section_content is None:
                raise FileNotFoundError(
                    f"Section '{section}' not found in {path}"
                )
            note = note.model_copy(update={"content": section_content})
        return note

    def update(
        self,
        path: str,
        content: str | None = None,
        mode: str = "replace",
        target: str | None = None,
        tags: List[str] | None = None,
        title: str | None = None,
    ) -> Note:
        note = self._read_file(path)
        now = datetime.now(timezone.utc)

        match mode:
            case "replace":
                new_content = content if content is not None else note.content
            case "append":
                new_content = note.content + "\n" + content if content is not None else note.content  # ← guarded
            case "prepend":
                new_content = content + "\n\n" + note.content if content is not None else note.content
            case "section":
                new_content = replace_section(note.content, target, content)
            case _:
                raise ValueError(f"Invalid mode: {mode}")

        updated = note.model_copy(update={
            "title": title if title is not None else note.title,
            "content": new_content,
            "updated": now,
            "tags": tags if tags is not None else note.tags,
        })
        self._write_file(updated)
        self._git_commit(f"folio: update ({mode}) {path}", [path])
        return updated

    def delete(self, path: str) -> None:
        filepath = self._resolve(path)
        if not filepath.exists():
            raise FileNotFoundError(f"Note not found: {path}")
        filepath.unlink()
        # Remove empty parent dirs up to root
        parent = filepath.parent
        while parent != self.root:
            if any(parent.iterdir()):
                break
            parent.rmdir()
            parent = parent.parent
        # Update index
        self._index.pop(path, None)
        self._git_commit(f"folio: delete {path}", [path])

    def move(self, source: str, target: str) -> Note:
        src_path = self._resolve(source)
        if not src_path.exists():
            raise FileNotFoundError(f"Note not found: {source}")
        tgt_path = self._resolve(target)
        if tgt_path.exists():
            raise FileExistsError(f"Target already exists: {target}")

        tgt_path.parent.mkdir(parents=True, exist_ok=True)
        
        if self._repo and self.git_enabled:
            try:
                # Use git mv to preserve history
                self._repo.git.mv(str(src_path.relative_to(self.root)), str(tgt_path.relative_to(self.root)))
            except Exception:
                shutil.move(str(src_path), str(tgt_path))
        else:
            shutil.move(str(src_path), str(tgt_path))

        # Clean empty source dirs
        parent = src_path.parent
        while parent != self.root:
            if any(parent.iterdir()):
                break
            parent.rmdir()
            parent = parent.parent

        # Read from new location, update index
        note = self._read_file(target)
        note = note.model_copy(update={
            "path": target,
            "updated": datetime.now(timezone.utc),
        })
        self._write_file(note)
        self._index.pop(source, None)
        self._index[target] = _IndexEntry(note)

        self._git_commit(f"folio: move {source} → {target}", [source, target])
        return note

    # ------------------------------------------------------------------
    # Backend methods: List
    # ------------------------------------------------------------------

    def list(self, folder: str | None = None) -> List[NoteSummary]:
        if folder:
            scan_dir = self._resolve(folder)
            if not scan_dir.exists() or not scan_dir.is_dir():
                return []
            files = scan_dir.rglob("*.md")
        else:
            files = self.root.rglob("*.md")

        summaries = []
        for filepath in sorted(files):
            rel = str(filepath.relative_to(self.root))
            try:
                note = self._read_file(rel)
                summaries.append(NoteSummary.from_note(
                    note,
                    has_previous=self._has_previous(rel),
                ))
            except Exception:
                continue

        # Sort by updated descending (most recent first)
        summaries.sort(key=lambda s: s.updated, reverse=True)
        return summaries

    # ------------------------------------------------------------------
    # Backend methods: Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        tags: List[str] | None = None,
        folder: str | None = None,
        sort: str = "relevance",
        updated_since: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> List[SearchResult]:
        query_lower = query.lower()
        query_terms = query_lower.split()
        cutoff = _parse_since(updated_since) if updated_since else None
        results: List[SearchResult] = []

        for path, entry in self._index.items():
            # --- Folder filter ---
            if folder:
                if not path.startswith(folder.rstrip("/") + "/"):
                    continue

            # --- Tag filter (must have ALL specified tags) ---
            if tags:
                entry_tags_lower = [t.lower() for t in entry.tags]
                if not all(t.lower() in entry_tags_lower for t in tags):
                    continue

            # --- Time filter ---
            if cutoff and entry.updated < cutoff:
                continue

            # --- Match scoring ---
            score = 0.0
            title_lower = entry.title.lower()

            # Title matches are worth more
            for term in query_terms:
                if term in title_lower:
                    score += 3.0
                if term in entry.content_lower:
                    score += 1.0
                    # Bonus for frequency
                    count = entry.content_lower.count(term)
                    score += min(count * 0.2, 2.0)  # cap frequency bonus

            if score == 0.0:
                continue

            # Filename match bonus
            stem_lower = Path(path).stem.lower()
            for term in query_terms:
                if term in stem_lower:
                    score += 2.0

            # Recency boost: notes updated in last 7 days get up to +1.0
            age_days = (datetime.now(timezone.utc) - entry.updated).days
            if age_days < 7:
                score += 1.0 * (1.0 - age_days / 7.0)

            # Build snippet (first matching line with context)
            snippet = _build_snippet(entry.content_lower, query_terms)

            summary = NoteSummary(
                path=entry.path,
                title=entry.title,
                tags=entry.tags,
                updated=entry.updated,
                size_tokens=entry.size_tokens,
            )
            results.append(SearchResult(note=summary, snippet=snippet, score=score))

        # --- Sort ---
        if sort == "recent":
            results.sort(key=lambda r: r.note.updated, reverse=True)
        else:
            results.sort(key=lambda r: r.score, reverse=True)

        return results[offset:offset+limit]

    # ------------------------------------------------------------------
    # Backend methods: Undo
    # ------------------------------------------------------------------

    def undo(self, path: str) -> Note:
        if not self._repo or not self.git_enabled:
            raise RuntimeError(
                "Undo requires git. Enable with FOLIO_LOCAL_GIT=true"
            )

        filepath = self._resolve(path)
        rel = str(Path(path))

        # Check there's a previous version
        try:
            commits = list(self._repo.iter_commits(paths=rel, max_count=2))
        except GitCommandError:
            raise FileNotFoundError(f"No git history for: {path}")

        if len(commits) < 2:
            raise ValueError(f"No previous version to undo: {path}")

        # Restore previous version
        try:
            self._repo.git.checkout("HEAD~1", "--", rel)
        except GitCommandError as e:
            raise RuntimeError(f"Git undo failed: {e}")

        # Commit the restoration
        self._repo.index.add([rel])
        self._repo.index.commit(f"folio: undo {path}")

        # Re-read and update index
        note = self._read_file(path)
        self._index[path] = _IndexEntry(note)
        return note

    # ------------------------------------------------------------------
    # Backend methods: Export / Import
    # ------------------------------------------------------------------

    def export_all(self) -> List[Note]:
        notes = []
        for filepath in sorted(self.root.rglob("*.md")):
            rel = str(filepath.relative_to(self.root))
            try:
                notes.append(self._read_file(rel))
            except Exception:
                continue
        return notes

    def import_all(self, notes: List[Note]) -> None:
        for note in notes:
            filepath = self._resolve(note.path)
            if filepath.exists():
                # Update existing
                self._write_file(note)
            else:
                # Create new
                self._write_file(note)
        self._build_index()
        self._git_commit("folio: import_all")


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _parse_dt(value: Any) -> datetime:
    """Parse a datetime from frontmatter. Handles strings and datetimes."""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        # Try ISO format
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def _parse_since(value: str) -> datetime:
    """Parse a time filter like '7d', '24h', 'today', or ISO date."""
    now = datetime.now(timezone.utc)

    if value.lower() == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Relative: '7d', '24h', '2w', '1m'
    match = re.match(r"^(\d+)([hdwm])$", value.lower())
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        delta = {
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
            "w": timedelta(weeks=amount),
            "m": timedelta(days=amount * 30),
        }[unit]
        return now - delta

    # ISO date
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        raise ValueError(
            f"Invalid time filter: '{value}'. "
            "Use relative ('7d', '24h', 'today') or ISO date."
        )


def _build_snippet(content_lower: str, terms: List[str], max_len: int = 150) -> str:
    """Build a search snippet showing the first matching line with context."""
    lines = content_lower.split("\n")

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("---"):
            continue
        for term in terms:
            if term in stripped:
                # Found a matching line — return it trimmed
                if len(stripped) > max_len:
                    # Find the term position and center the snippet
                    pos = stripped.find(term)
                    start = max(0, pos - max_len // 2)
                    end = min(len(stripped), start + max_len)
                    return "..." + stripped[start:end] + "..."
                return stripped

    # No line match — return first non-empty line
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("---"):
            return stripped[:max_len]

    return ""
