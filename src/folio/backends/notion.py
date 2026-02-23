"""Notion backend — cloud-hosted notes via Notion API.

Single-database approach: all notes live in one Notion database.
Folders are a 'folder' select property. Paths are a 'folio_path' text property.
The path→page_id mapping is bulk-loaded on startup (1 API call per 100 notes)
and kept in sync via an in-memory cache.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from notion_client import Client as NotionClient
from notion_client.errors import APIResponseError

from folio.backends import FolioBackend
from folio.config import NotionConfig
from folio.models import Note, NoteSummary, SearchResult
from folio.sections import extract_section, replace_section


class NotionBackend(FolioBackend):

    def __init__(self, config: NotionConfig):
        self.client = NotionClient(auth=config.api_key)
        self.database_id = config.database_id
        self._cache: dict[str, str] = {}  # path → page_id
        self._ensure_schema()
        self._load_cache()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Verify the database has required properties, create if missing."""
        try:
            db = self.client.databases.retrieve(self.database_id)
        except APIResponseError as e:
            raise ConnectionError(
                f"Cannot access Notion database: {e.message}. "
                "Check NOTION_DATABASE_ID and that the integration has access."
            )

        props = db.get("properties", {})
        updates: dict[str, Any] = {}

        if "folio_path" not in props:
            updates["folio_path"] = {"rich_text": {}}
        if "folder" not in props:
            updates["folder"] = {"select": {}}
        if "tags" not in props:
            updates["tags"] = {"multi_select": {}}

        if updates:
            self.client.databases.update(
                database_id=self.database_id,
                properties=updates,
            )

    def _load_cache(self) -> None:
        """Bulk-load ALL path→page_id mappings on startup.

        100 notes = 1 API call. 500 notes = 5 API calls.
        Runs once. After this, path resolution is in-memory.
        """
        self._cache.clear()
        cursor = None

        while True:
            response = self.client.databases.query(
                database_id=self.database_id,
                start_cursor=cursor,
                page_size=100,
            )

            for page in response["results"]:
                if page.get("archived"):
                    continue
                path = self._get_path(page)
                if path:
                    self._cache[path] = page["id"]

            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def _resolve_page_id(self, path: str) -> str:
        """Get Notion page ID for a path. Cache-first with fallback."""
        if path in self._cache:
            return self._cache[path]

        # Cache miss — maybe created externally in Notion UI
        response = self.client.databases.query(
            database_id=self.database_id,
            filter={
                "property": "folio_path",
                "rich_text": {"equals": path},
            },
        )

        results = [r for r in response["results"] if not r.get("archived")]
        if results:
            page_id = results[0]["id"]
            self._cache[path] = page_id
            return page_id

        raise FileNotFoundError(f"Note not found: {path}")

    # ------------------------------------------------------------------
    # Property readers
    # ------------------------------------------------------------------

    def _get_path(self, page: dict) -> str | None:
        """Extract folio_path from a page object."""
        prop = page.get("properties", {}).get("folio_path", {})
        rich_text = prop.get("rich_text", [])
        if rich_text:
            return rich_text[0].get("plain_text", "")
        return None

    def _get_title_from_page(self, page: dict) -> str:
        """Extract title from a page object (handles any title property name)."""
        for key, prop in page.get("properties", {}).items():
            if prop.get("type") == "title":
                title_arr = prop.get("title", [])
                if title_arr:
                    return title_arr[0].get("plain_text", "Untitled")
        return "Untitled"

    def _get_tags_from_page(self, page: dict) -> list[str]:
        """Extract tags from a page object."""
        prop = page.get("properties", {}).get("tags", {})
        options = prop.get("multi_select", [])
        return [o.get("name", "") for o in options]

    def _get_timestamps(self, page: dict) -> tuple[datetime, datetime]:
        """Extract created and updated from Notion's auto-managed fields."""
        created_str = page.get("created_time", "")
        updated_str = page.get("last_edited_time", "")
        created = (
            datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            if created_str
            else datetime.now(timezone.utc)
        )
        updated = (
            datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
            if updated_str
            else datetime.now(timezone.utc)
        )
        return created, updated

    # ------------------------------------------------------------------
    # Property builder
    # ------------------------------------------------------------------

    def _build_properties(self, note: Note) -> dict:
        """Build Notion page properties dict from a Note."""
        folder = str(Path(note.path).parent) if "/" in note.path else ""

        props: dict[str, Any] = {
            "Name": {"title": [{"text": {"content": note.title}}]},
            "folio_path": {
                "rich_text": [{"text": {"content": note.path}}]
            },
            "tags": {
                "multi_select": [{"name": t} for t in note.tags]
            },
        }

        # Only set folder if there is one (avoid empty select)
        if folder:
            props["folder"] = {"select": {"name": folder}}

        return props

    # ------------------------------------------------------------------
    # Page content I/O
    # ------------------------------------------------------------------

    def _read_page_content(self, page_id: str) -> str:
        """Read all blocks from a page, convert to markdown."""
        blocks: list[dict] = []
        cursor = None

        while True:
            response = self.client.blocks.children.list(
                block_id=page_id,
                start_cursor=cursor,
                page_size=100,
            )
            blocks.extend(response["results"])

            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")

        return _blocks_to_markdown(blocks)

    def _write_page_content(self, page_id: str, markdown: str) -> None:
        """Replace all page content with new markdown."""
        self._clear_page_blocks(page_id)
        blocks = _markdown_to_blocks(markdown)
        if blocks:
            self._append_blocks(page_id, blocks)

    def _append_page_content(self, page_id: str, markdown: str) -> None:
        """Append markdown as new blocks at end of page."""
        blocks = _markdown_to_blocks(markdown)
        if blocks:
            self._append_blocks(page_id, blocks)

    def _clear_page_blocks(self, page_id: str) -> None:
        """Delete all blocks from a page."""
        cursor = None
        while True:
            response = self.client.blocks.children.list(
                block_id=page_id,
                start_cursor=cursor,
                page_size=100,
            )
            for block in response["results"]:
                try:
                    self.client.blocks.delete(block_id=block["id"])
                except APIResponseError:
                    pass
            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")

    def _append_blocks(self, page_id: str, blocks: list[dict]) -> None:
        """Append blocks to a page, batching in groups of 100 (API limit)."""
        for i in range(0, len(blocks), 100):
            batch = blocks[i : i + 100]
            self.client.blocks.children.append(
                block_id=page_id,
                children=batch,
            )

    def _page_to_note(self, page: dict, content: str | None = None) -> Note:
        """Convert a Notion page + content into a Note."""
        path = self._get_path(page) or "unknown.md"
        tags = self._get_tags_from_page(page)
        created, updated = self._get_timestamps(page)

        if content is None:
            content = self._read_page_content(page["id"])

        return Note(
            path=path,
            content=content,
            tags=tags,
            created=created,
            updated=updated,
        )

    # ------------------------------------------------------------------
    # CRUD: Create
    # ------------------------------------------------------------------

    def create(self, note: Note) -> Note:
        if note.path in self._cache:
            raise FileExistsError(f"Note already exists: {note.path}")

        # Create page with properties (no content yet)
        properties = self._build_properties(note)
        page = self.client.pages.create(
            parent={"database_id": self.database_id},
            properties=properties,
        )
        page_id = page["id"]

        # Append content as blocks
        if note.content:
            blocks = _markdown_to_blocks(note.content)
            if blocks:
                self._append_blocks(page_id, blocks)

        # Cache the new mapping
        self._cache[note.path] = page_id

        created, updated = self._get_timestamps(page)
        return note.model_copy(update={"created": created, "updated": updated})

    # ------------------------------------------------------------------
    # CRUD: Read
    # ------------------------------------------------------------------

    def read(self, path: str, section: str | None = None) -> Note:
        page_id = self._resolve_page_id(path)

        # Fetch page (properties + timestamps)
        try:
            page = self.client.pages.retrieve(page_id)
        except APIResponseError:
            self._cache.pop(path, None)
            raise FileNotFoundError(f"Note not found: {path}")

        if page.get("archived"):
            self._cache.pop(path, None)
            raise FileNotFoundError(f"Note was deleted: {path}")

        # Fetch content blocks → markdown
        note = self._page_to_note(page)

        # Section extraction (uses shared sections.py)
        if section:
            section_content = extract_section(note.content, section)
            if section_content is None:
                raise FileNotFoundError(
                    f"Section '{section}' not found in {path}"
                )
            note = note.model_copy(update={"content": section_content})

        return note

    # ------------------------------------------------------------------
    # CRUD: Update
    # ------------------------------------------------------------------

    def update(
        self,
        path: str,
        content: str,
        mode: str = "replace",
        target: str | None = None,
        tags: list[str] | None = None,
    ) -> Note:
        page_id = self._resolve_page_id(path)

        # Fetch current state
        try:
            page = self.client.pages.retrieve(page_id)
        except APIResponseError:
            self._cache.pop(path, None)
            raise FileNotFoundError(f"Note not found: {path}")

        match mode:
            case "replace":
                # Rewrite all blocks
                self._write_page_content(page_id, content)

            case "append":
                # Add new blocks at the end
                self._append_page_content(page_id, content)

            case "section":
                # Read current content, replace section, rewrite
                current_md = self._read_page_content(page_id)
                new_md = replace_section(current_md, target, content)
                self._write_page_content(page_id, new_md)

            case _:
                raise ValueError(f"Invalid mode: {mode}")

        # Update tags if provided
        if tags is not None:
            self.client.pages.update(
                page_id=page_id,
                properties={
                    "tags": {
                        "multi_select": [{"name": t} for t in tags]
                    }
                },
            )

        # Re-fetch to get updated timestamps
        page = self.client.pages.retrieve(page_id)
        return self._page_to_note(page)

    # ------------------------------------------------------------------
    # CRUD: Delete
    # ------------------------------------------------------------------

    def delete(self, path: str) -> None:
        page_id = self._resolve_page_id(path)

        try:
            # Notion "delete" = archive
            self.client.pages.update(
                page_id=page_id,
                archived=True,
            )
        except APIResponseError as e:
            raise RuntimeError(f"Failed to delete: {e.message}")

        # Remove from cache
        self._cache.pop(path, None)

    # ------------------------------------------------------------------
    # Move
    # ------------------------------------------------------------------

    def move(self, source: str, target: str) -> Note:
        """Move = update path + folder properties. Page stays the same."""
        page_id = self._resolve_page_id(source)

        # Check target doesn't already exist
        if target in self._cache:
            raise FileExistsError(f"Target already exists: {target}")

        # Derive new folder and title from target path
        new_folder = str(Path(target).parent) if "/" in target else ""
        new_stem = Path(target).stem
        new_title = new_stem.replace("-", " ").replace("_", " ").title()

        # Update properties on the existing page
        props: dict[str, Any] = {
            "folio_path": {
                "rich_text": [{"text": {"content": target}}]
            },
        }

        if new_folder:
            props["folder"] = {"select": {"name": new_folder}}
        else:
            # Clear folder — set to empty by removing the select
            props["folder"] = {"select": None}

        try:
            self.client.pages.update(
                page_id=page_id,
                properties=props,
            )
        except APIResponseError as e:
            raise RuntimeError(f"Failed to move: {e.message}")

        # Update cache: remove old, add new
        self._cache.pop(source, None)
        self._cache[target] = page_id

        # Re-fetch and return
        page = self.client.pages.retrieve(page_id)
        return self._page_to_note(page)

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    def list(self, folder: str | None = None) -> list[NoteSummary]:
        """List notes, optionally filtered by folder.

        Uses a database query with filter (not the cache) so we get
        fresh data including titles, tags, and timestamps.
        """
        query_args: dict[str, Any] = {
            "database_id": self.database_id,
            "page_size": 100,
            "sorts": [
                {
                    "timestamp": "last_edited_time",
                    "direction": "descending",
                }
            ],
        }

        # Folder filter
        if folder:
            query_args["filter"] = {
                "property": "folder",
                "select": {"equals": folder.rstrip("/")},
            }

        summaries: list[NoteSummary] = []
        cursor = None

        while True:
            if cursor:
                query_args["start_cursor"] = cursor

            response = self.client.databases.query(**query_args)

            for page in response["results"]:
                if page.get("archived"):
                    continue

                path = self._get_path(page)
                if not path:
                    continue

                title = self._get_title_from_page(page)
                tags = self._get_tags_from_page(page)
                _, updated = self._get_timestamps(page)

                summaries.append(NoteSummary(
                    path=path,
                    title=title,
                    tags=tags,
                    updated=updated,
                    size_tokens=0,      # would need a content fetch to know
                    has_previous=False,  # no git in Notion
                ))

                # Keep cache fresh while we're at it
                self._cache[path] = page["id"]

            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")

        return summaries

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        tags: list[str] | None = None,
        folder: str | None = None,
        sort: str = "relevance",
        updated_since: str | None = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        """Search notes using Notion's search API + database filters.

        Strategy: Use Notion's search for content matching, then apply
        tag/folder/time filters locally on the results. This minimizes
        API calls while still supporting all filter combinations.
        """
        # Step 1: Content search via Notion search API
        search_response = self.client.search(
            query=query,
            filter={"property": "object", "value": "page"},
            sort={
                "direction": "descending",
                "timestamp": "last_edited_time",
            },
            page_size=100,  # fetch more than limit to allow for filtering
        )

        # Step 2: Filter to only pages in our database
        our_db = self.database_id.replace("-", "")
        candidates: list[dict] = []

        for page in search_response.get("results", []):
            if page.get("archived"):
                continue

            # Check page belongs to our database
            parent = page.get("parent", {})
            parent_db = parent.get("database_id", "").replace("-", "")
            if parent_db != our_db:
                continue

            path = self._get_path(page)
            if not path:
                continue

            candidates.append(page)

        # Step 3: Apply local filters
        cutoff = _parse_since(updated_since) if updated_since else None
        results: list[SearchResult] = []

        for page in candidates:
            path = self._get_path(page)
            page_tags = self._get_tags_from_page(page)
            _, updated = self._get_timestamps(page)
            title = self._get_title_from_page(page)

            # --- Tag filter (must have ALL specified tags) ---
            if tags:
                page_tags_lower = [t.lower() for t in page_tags]
                if not all(t.lower() in page_tags_lower for t in tags):
                    continue

            # --- Folder filter ---
            if folder:
                if not path.startswith(folder.rstrip("/") + "/"):
                    continue

            # --- Time filter ---
            if cutoff and updated < cutoff:
                continue

            # --- Build snippet (first ~150 chars of content) ---
            # We avoid fetching full content for every result.
            # Use title + path as the snippet for now.
            snippet = f"{title} ({path})"

            # --- Score ---
            # Notion's search already ranks by relevance.
            # We use position in results as a proxy score.
            position_score = 1.0 - (len(results) * 0.05)

            # Title match bonus
            query_lower = query.lower()
            if query_lower in title.lower():
                position_score += 2.0

            summary = NoteSummary(
                path=path,
                title=title,
                tags=page_tags,
                updated=updated,
                size_tokens=0,
                has_previous=False,
            )

            results.append(SearchResult(
                note=summary,
                snippet=snippet,
                score=max(position_score, 0.1),
            ))

            if len(results) >= limit:
                break

        # Step 4: Sort
        if sort == "recent":
            results.sort(key=lambda r: r.note.updated, reverse=True)
        else:
            results.sort(key=lambda r: r.score, reverse=True)

        return results[:limit]

    # ------------------------------------------------------------------
    # Undo
    # ------------------------------------------------------------------

    def undo(self, path: str) -> Note:
        """Undo is not supported with Notion backend.

        Notion has its own page history (available on paid plans),
        but it's not exposed via the API. We raise a clear error
        so the agent knows to tell the user.
        """
        raise RuntimeError(
            f"Undo is not available with the Notion backend. "
            f"Notion maintains its own page history — use Notion's UI "
            f"to view previous versions of '{path}'."
        )

    # ------------------------------------------------------------------
    # Export / Import
    # ------------------------------------------------------------------

    def export_all(self) -> list[Note]:
        """Export all notes as a list of Note objects.

        Fetches every page + its content. Useful for migration
        (e.g., Notion → local) or backup.
        """
        notes: list[Note] = []
        cursor = None

        while True:
            response = self.client.databases.query(
                database_id=self.database_id,
                start_cursor=cursor,
                page_size=100,
            )

            for page in response["results"]:
                if page.get("archived"):
                    continue
                path = self._get_path(page)
                if not path:
                    continue
                try:
                    note = self._page_to_note(page)
                    notes.append(note)
                except Exception:
                    continue  # skip pages we can't read

            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")

        return notes

    def import_all(self, notes: list[Note]) -> None:
        """Import notes into the Notion database.

        Creates new pages for notes that don't exist.
        Updates existing pages for notes that already exist.
        """
        for note in notes:
            if note.path in self._cache:
                # Update existing
                self.update(
                    path=note.path,
                    content=note.content,
                    mode="replace",
                    tags=note.tags,
                )
            else:
                # Create new
                self.create(note)


# =========================================================================
# Module-level helper
# =========================================================================

def _parse_since(value: str) -> datetime:
    """Parse a time filter like '7d', '24h', 'today', or ISO date."""
    now = datetime.now(timezone.utc)

    if value.lower() == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

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


# =========================================================================
# Module-level: Markdown ↔ Notion Blocks
# =========================================================================

def _text_segment(
    content: str,
    bold: bool = False,
    italic: bool = False,
    code: bool = False,
) -> dict:
    return {
        "type": "text",
        "text": {"content": content},
        "annotations": {
            "bold": bold, "italic": italic, "code": code,
            "strikethrough": False, "underline": False, "color": "default",
        },
    }


def _link_segment(text: str, url: str) -> dict:
    return {
        "type": "text",
        "text": {"content": text, "link": {"url": url}},
        "annotations": {
            "bold": False, "italic": False, "code": False,
            "strikethrough": False, "underline": False, "color": "default",
        },
    }


_INLINE_RE = re.compile(
    r"(\*\*(.+?)\*\*)"
    r"|(\*(.+?)\*)"
    r"|(`(.+?)`)"
    r"|(\[(.+?)\]\((.+?)\))"
)


def _parse_inline(text: str) -> list[dict]:
    """Parse **bold**, *italic*, `code`, [text](url) into rich_text."""
    if not text:
        return [_text_segment("")]

    segments: list[dict] = []
    last_end = 0

    for m in _INLINE_RE.finditer(text):
        if m.start() > last_end:
            segments.append(_text_segment(text[last_end:m.start()]))
        if m.group(2):
            segments.append(_text_segment(m.group(2), bold=True))
        elif m.group(4):
            segments.append(_text_segment(m.group(4), italic=True))
        elif m.group(6):
            segments.append(_text_segment(m.group(6), code=True))
        elif m.group(8):
            segments.append(_link_segment(m.group(8), m.group(9)))
        last_end = m.end()

    if last_end < len(text):
        segments.append(_text_segment(text[last_end:]))

    return segments if segments else [_text_segment(text)]


def _markdown_to_blocks(markdown: str) -> list[dict]:
    """Convert markdown string to Notion block objects."""
    lines = markdown.split("\n")
    blocks: list[dict] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # --- Fenced code block ---
        if stripped.startswith("```"):
            lang = stripped[3:].strip() or "plain text"
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            blocks.append({
                "type": "code",
                "code": {
                    "rich_text": [_text_segment("\n".join(code_lines))],
                    "language": lang,
                },
            })
            continue

        # --- Heading ---
        h_match = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if h_match:
            level = len(h_match.group(1))
            btype = f"heading_{level}"
            blocks.append({
                "type": btype,
                btype: {"rich_text": _parse_inline(h_match.group(2).strip())},
            })
            i += 1
            continue

        # --- Divider ---
        if stripped in ("---", "***", "___"):
            blocks.append({"type": "divider", "divider": {}})
            i += 1
            continue

        # --- Bullet list ---
        b_match = re.match(r"^\s*[-*]\s+(.+)$", line)
        if b_match:
            blocks.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {
                    "rich_text": _parse_inline(b_match.group(1).strip()),
                },
            })
            i += 1
            continue

        # --- Numbered list ---
        n_match = re.match(r"^\s*\d+\.\s+(.+)$", line)
        if n_match:
            blocks.append({
                "type": "numbered_list_item",
                "numbered_list_item": {
                    "rich_text": _parse_inline(n_match.group(1).strip()),
                },
            })
            i += 1
            continue

        # --- Blockquote ---
        q_match = re.match(r"^>\s*(.*)", line)
        if q_match:
            blocks.append({
                "type": "quote",
                "quote": {
                    "rich_text": _parse_inline(q_match.group(1).strip()),
                },
            })
            i += 1
            continue

        # --- Empty line (skip) ---
        if not stripped:
            i += 1
            continue

        # --- Paragraph (default) ---
        blocks.append({
            "type": "paragraph",
            "paragraph": {"rich_text": _parse_inline(stripped)},
        })
        i += 1

    return blocks


def _blocks_to_markdown(blocks: list[dict]) -> str:
    """Convert Notion blocks back to markdown."""
    lines: list[str] = []
    prev_type = ""

    for block in blocks:
        btype = block.get("type", "")
        data = block.get(btype, {})
        rt = data.get("rich_text", [])

        # Add blank line when switching away from list items
        if prev_type in ("bulleted_list_item", "numbered_list_item"):
            if btype not in ("bulleted_list_item", "numbered_list_item"):
                lines.append("")

        match btype:
            case "heading_1":
                lines.append(f"# {_rt_to_md(rt)}")
                lines.append("")
            case "heading_2":
                lines.append(f"## {_rt_to_md(rt)}")
                lines.append("")
            case "heading_3":
                lines.append(f"### {_rt_to_md(rt)}")
                lines.append("")
            case "paragraph":
                text = _rt_to_md(rt)
                lines.append(text)
                lines.append("")
            case "bulleted_list_item":
                lines.append(f"- {_rt_to_md(rt)}")
            case "numbered_list_item":
                lines.append(f"1. {_rt_to_md(rt)}")
            case "quote":
                lines.append(f"> {_rt_to_md(rt)}")
                lines.append("")
            case "code":
                lang = data.get("language", "")
                if lang == "plain text":
                    lang = ""
                code = _rt_to_plain(rt)
                lines.append(f"```{lang}")
                lines.append(code)
                lines.append("```")
                lines.append("")
            case "divider":
                lines.append("---")
                lines.append("")
            case _:
                if rt:
                    lines.append(_rt_to_md(rt))
                    lines.append("")

        prev_type = btype

    result = "\n".join(lines)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _rt_to_md(rich_text: list[dict]) -> str:
    """Convert Notion rich_text array to markdown with formatting."""
    parts: list[str] = []
    for seg in rich_text:
        text = seg.get("text", {}).get("content", "")
        ann = seg.get("annotations", {})
        link = seg.get("text", {}).get("link")

        if link:
            text = f"[{text}]({link.get('url', '')})"
        elif ann.get("code"):
            text = f"`{text}`"
        elif ann.get("bold") and ann.get("italic"):
            text = f"***{text}***"
        elif ann.get("bold"):
            text = f"**{text}**"
        elif ann.get("italic"):
            text = f"*{text}*"

        parts.append(text)
    return "".join(parts)


def _rt_to_plain(rich_text: list[dict]) -> str:
    """Extract plain text from rich_text (no formatting)."""
    return "".join(seg.get("text", {}).get("content", "") for seg in rich_text)
