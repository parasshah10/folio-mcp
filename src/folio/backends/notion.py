"""Notion backend — cloud-hosted notes via Notion API.

Single-database approach: all notes live in one Notion database.
Folders are a 'folder' rich_text property. Paths are a 'folio_path' text property.
The path→page_id mapping is bulk-loaded on startup (1 API call per 100 notes)
and kept in sync via an in-memory cache.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, List

from notion_client import Client as NotionClient
from notion_client.errors import APIResponseError

from folio.backends import FolioBackend
from folio.config import NotionConfig
from folio.models import Note, NoteSummary, SearchResult
from folio.sections import extract_section, replace_section


def _slugify_title(title: str) -> str:
    """Convert a page title to a Folio-style path slug."""
    slug = title.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)  # Remove special chars
    slug = re.sub(r"[\s_]+", "-", slug)  # Spaces/underscores → hyphens
    slug = re.sub(r"-+", "-", slug).strip("-")  # Collapse hyphens
    return f"{slug}.md" if slug else None


class NotionBackend(FolioBackend):

    def __init__(self, config: NotionConfig):
        self.client = NotionClient(
            auth=config.api_key,
            notion_version="2025-09-03"
        )
        self.database_id = config.database_id
        self.data_source_id: str | None = None
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
            # Find the primary data source ID
            if "data_sources" in db and db["data_sources"]:
                self.data_source_id = db["data_sources"][0]["id"]
            else:
                # Fallback to database_id if no data_sources (standard database)
                self.data_source_id = self.database_id
        except APIResponseError as e:
            raise ConnectionError(
                f"Cannot access Notion database: {str(e)}. "
                "Check NOTION_DATABASE_ID and that the integration has access."
            )

        props = db.get("properties", {})
        updates: dict[str, Any] = {}

        if "folio_path" not in props:
            updates["folio_path"] = {"rich_text": {}}
        if "folder" not in props:
            updates["folder"] = {"rich_text": {}}
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
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor

            response = self.client.request(
                path=f"data_sources/{self.data_source_id}/query",
                method="POST",
                body=body,
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
        response = self.client.request(
            path=f"data_sources/{self.data_source_id}/query",
            method="POST",
            body={
                "filter": {
                    "property": "folio_path",
                    "rich_text": {"equals": path},
                },
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

    def _get_folder(self, page: dict) -> str:
        """Extract folder from a page object."""
        prop = page.get("properties", {}).get("folder", {})
        rich_text = prop.get("rich_text", [])
        if rich_text:
            return rich_text[0].get("plain_text", "")
        return ""

    def _get_title_from_page(self, page: dict) -> str:
        """Extract title from a page object (handles any title property name)."""
        for key, prop in page.get("properties", {}).items():
            if prop.get("type") == "title":
                title_arr = prop.get("title", [])
                if title_arr:
                    return title_arr[0].get("plain_text", "Untitled")
        return "Untitled"

    def _get_tags_from_page(self, page: dict) -> List[str]:
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
            props["folder"] = {"rich_text": [{"text": {"content": folder}}]}

        return props

    # ------------------------------------------------------------------
    # Page content I/O
    # ------------------------------------------------------------------

    def _fetch_all_blocks(self, page_id: str) -> List[dict]:
        """Fetch all block objects for a page, skipping archived ones."""
        blocks: List[dict] = []
        cursor = None

        while True:
            response = self.client.blocks.children.list(
                block_id=page_id,
                start_cursor=cursor,
                page_size=100,
            )
            for b in response["results"]:
                if not b.get("archived") and not b.get("in_trash"):
                    blocks.append(b)
                    # Recursively fetch rows for tables
                    if b.get("type") == "table" and b.get("has_children"):
                        rows = self._fetch_all_blocks(b["id"])
                        blocks.extend(rows)

            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")

        return blocks

    def _read_page_content(self, page_id: str) -> str:
        """Read all blocks from a page, convert to markdown."""
        blocks = self._fetch_all_blocks(page_id)
        return _blocks_to_markdown(blocks)

    def _write_page_content(self, page_id: str, markdown: str) -> None:
        """Replace all page content with new markdown."""
        self._clear_page_blocks(page_id)
        if markdown:
            content = markdown.replace("\\n", "\n")
            blocks = _markdown_to_blocks(content)
            if blocks:
                self._append_blocks(page_id, blocks)

    def _append_page_content(self, page_id: str, markdown: str) -> None:
        """Append markdown as new blocks at end of page."""
        if markdown:
            content = markdown.replace("\\n", "\n")
            blocks = _markdown_to_blocks(content)
            if blocks:
                self._append_blocks(page_id, blocks)

    def _clear_page_blocks(self, page_id: str) -> None:
        """Delete all blocks from a page."""
        blocks = self._fetch_all_blocks(page_id)
        self._delete_blocks([b["id"] for b in blocks])

    def _delete_blocks(self, block_ids: List[str]) -> None:
        """Delete specific blocks one-by-one."""
        for block_id in block_ids:
            try:
                self.client.request(
                    path=f"blocks/{block_id}",
                    method="DELETE"
                )
            except APIResponseError as e:
                raise RuntimeError(f"Failed to delete block {block_id}: {str(e)}")

    def _insert_blocks(self, parent_id: str, blocks: List[dict], after_id: str | None = None) -> None:
        """Insert blocks after a specific block ID, or at end if None."""
        for i in range(0, len(blocks), 100):
            batch = blocks[i : i + 100]
            kwargs: dict[str, Any] = {
                "block_id": parent_id,
                "children": batch,
            }
            if after_id:
                kwargs["position"] = {
                    "type": "after_block",
                    "after_block": {"id": after_id}
                }
            
            resp = self.client.blocks.children.append(**kwargs)
            
            # Update after_id to the LAST block of the batch for sequential insertion
            if i + 100 < len(blocks) and resp.get("results"):
                after_id = resp["results"][-1]["id"]

    def _append_blocks(self, page_id: str, blocks: List[dict]) -> None:
        """Append blocks to a page, batching in groups of 100 (API limit)."""
        self._insert_blocks(page_id, blocks)

    def _update_section(self, page_id: str, target: str, content: str) -> None:
        """Find a section by heading, delete its blocks, and insert new ones."""
        all_blocks = self._fetch_all_blocks(page_id)
        target_lower = target.strip().lower()
        
        start_idx = -1
        heading_level = -1
        end_idx = -1
        
        for i, block in enumerate(all_blocks):
            btype = block.get("type", "")
            if btype in ("heading_1", "heading_2", "heading_3"):
                level = int(btype[-1])
                text = _rt_to_plain(block[btype].get("rich_text", [])).strip().lower()
                
                if start_idx == -1:
                    if text == target_lower:
                        start_idx = i
                        heading_level = level
                else:
                    if level <= heading_level:
                        end_idx = i
                        break

        if start_idx == -1:
            raise ValueError(f"Section '{target}' not found")

        if end_idx == -1:
            end_idx = len(all_blocks)

        # Blocks to delete
        to_delete = [b["id"] for b in all_blocks[start_idx + 1:end_idx]]
        
        # Block BEFORE the section (if any)
        after_id = all_blocks[start_idx]["id"]
        
        # 1. Delete the blocks
        self._delete_blocks(to_delete)
        
        # 2. Insert new blocks
        if content:
            content_fixed = content.replace("\\n", "\n")
            new_blocks = _markdown_to_blocks(content_fixed)
            if new_blocks:
                self._insert_blocks(page_id, new_blocks, after_id=after_id)

    def _page_to_note(self, page: dict, content: str | None = None) -> Note:
        """Convert a Notion page + content into a Note."""
        folio_path = self._get_path(page)
        title = self._get_title_from_page(page)
        folder = self._get_folder(page)

        # Path derivation for notes created directly in Notion UI
        if not folio_path:
            slug = _slugify_title(title) if title else f"untitled-{page['id'][:8]}.md"
            folio_path = f"{folder}/{slug}" if folder else slug
            
            # Write derived path back to Notion so it's stable
            try:
                self.client.pages.update(
                    page_id=page["id"],
                    properties={
                        "folio_path": {"rich_text": [{"text": {"content": folio_path}}]}
                    }
                )
                self._cache[folio_path] = page["id"]
            except Exception as e:
                import logging
                logging.getLogger("folio.notion").error(f"Failed to write derived path back to Notion: {e}")

        created, updated = self._get_timestamps(page)

        # Content is ALWAYS fetched fresh to avoid race conditions/stale data
        content = self._read_page_content(page["id"])

        return Note(
            path=folio_path,
            content=content,
            tags=self._get_tags_from_page(page),
            created=created,
            updated=updated,
            metadata={"title": title}
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
        
        # Log the URL so the user can find the page immediately
        import sys
        print(f"\n[folio] CREATED PAGE: {page.get('url')}\n", file=sys.stderr)

        # Append content as blocks
        if note.content:
            content_fixed = note.content.replace("\\n", "\n")
            blocks = _markdown_to_blocks(content_fixed)
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
        content: str | None,                    # ← was: str
        mode: str = "replace",
        target: str | None = None,
        tags: List[str] | None = None,
    ) -> Note:
        page_id = self._resolve_page_id(path)

        try:
            page = self.client.pages.retrieve(page_id)
        except APIResponseError:
            self._cache.pop(path, None)
            raise FileNotFoundError(f"Note not found: {path}")

        # --- Content update (skip if None → retag only) ---
        if content is not None:                  # ← wrap the whole block
            match mode:
                case "replace":
                    self._write_page_content(page_id, content)
                case "append":
                    self._append_page_content(page_id, content)
                case "section":
                    self._update_section(page_id, target, content)
                case _:
                    raise ValueError(f"Invalid mode: {mode}")

        # --- Tag update ---
        if tags is not None:
            self.client.pages.update(
                page_id=page_id,
                properties={
                    "tags": {
                        "multi_select": [{"name": t} for t in tags]
                    }
                },
            )

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
            raise RuntimeError(f"Failed to delete: {str(e)}")

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
            "Name": {"title": [{"text": {"content": new_title}}]},
            "folio_path": {
                "rich_text": [{"text": {"content": target}}]
            },
        }

        if new_folder:
            props["folder"] = {"rich_text": [{"text": {"content": new_folder}}]}
        else:
            # Clear folder
            props["folder"] = {"rich_text": []}

        try:
            self.client.pages.update(
                page_id=page_id,
                properties=props,
            )
        except APIResponseError as e:
            raise RuntimeError(f"Failed to move: {str(e)}")

        # Update cache: remove old, add new
        self._cache.pop(source, None)
        self._cache[target] = page_id

        # Re-fetch and return
        page = self.client.pages.retrieve(page_id)
        return self._page_to_note(page)

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    def list(self, folder: str | None = None) -> List[NoteSummary]:
        """List notes, optionally filtered by folder.

        Uses a database query with filter (not the cache) so we get
        fresh data including titles, tags, and timestamps.
        """
        body: dict[str, Any] = {
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
            body["filter"] = {
                "property": "folder",
                "rich_text": {"equals": folder.rstrip("/")},
            }

        summaries: List[NoteSummary] = []
        cursor = None

        while True:
            if cursor:
                body["start_cursor"] = cursor

            response = self.client.request(
                path=f"data_sources/{self.data_source_id}/query",
                method="POST",
                body=body,
            )

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
        tags: List[str] | None = None,
        folder: str | None = None,
        sort: str = "relevance",
        updated_since: str | None = None,
        limit: int = 10,
    ) -> List[SearchResult]:
        """Search notes by title, tags, folder, and time.

        Uses Notion's databases.query() with property filters only.
        No client-side content matching — every search is a single API call.
        """
        # 1. Build Property Filters
        and_filters = []

        if query:
            and_filters.append({
                "property": "title",
                "title": {"contains": query}
            })

        if tags:
            for tag in tags:
                and_filters.append({
                    "property": "tags",
                    "multi_select": {"contains": tag}
                })

        if folder:
            and_filters.append({
                "property": "folder",
                "rich_text": {"equals": folder.rstrip("/")}
            })

        filter_obj = None
        if and_filters:
            filter_obj = {"and": and_filters} if len(and_filters) > 1 else and_filters[0]

        # 2. Query Database (single API call)
        body: dict[str, Any] = {"page_size": min(limit * 2, 100)}
        if filter_obj:
            body["filter"] = filter_obj

        if sort == "recent":
            body["sorts"] = [{"timestamp": "last_edited_time", "direction": "descending"}]
        else:
            body["sorts"] = [{"timestamp": "last_edited_time", "direction": "descending"}]

        try:
            response = self.client.request(
                path=f"data_sources/{self.data_source_id}/query",
                method="POST",
                body=body,
            )
        except APIResponseError as e:
            raise RuntimeError(f"Search query failed: {str(e)}")

        pages = response.get("results", [])
        cutoff = _parse_since(updated_since) if updated_since else None
        results: List[SearchResult] = []

        # 3. Process results
        for page in pages:
            if page.get("archived"):
                continue

            path = self._get_path(page)
            if not path:
                continue

            title = self._get_title_from_page(page)
            page_tags = self._get_tags_from_page(page)
            _, updated = self._get_timestamps(page)

            if cutoff and updated < cutoff:
                continue

            summary = NoteSummary(
                path=path,
                title=title,
                tags=page_tags,
                updated=updated,
                size_tokens=0,
            )

            results.append(SearchResult(
                note=summary,
                snippet=title,
                score=1.0,
            ))

            if len(results) >= limit:
                break

        return results

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

    def export_all(self) -> List[Note]:
        """Export all notes as a list of Note objects.

        Fetches every page + its content. Useful for migration
        (e.g., Notion → local) or backup.
        """
        notes: List[Note] = []
        cursor = None

        while True:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor

            response = self.client.request(
                path=f"data_sources/{self.data_source_id}/query",
                method="POST",
                body=body,
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

    def import_all(self, notes: List[Note]) -> None:
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


def _parse_inline(text: str) -> List[dict]:
    """Parse **bold**, *italic*, `code`, [text](url) into rich_text."""
    if not text:
        return [_text_segment("")]

    segments: List[dict] = []
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


def _markdown_to_blocks(markdown: str) -> List[dict]:
    """Convert markdown string to Notion block objects."""
    lines = markdown.split("\n")
    blocks: List[dict] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # --- Table ---
        if stripped.startswith("|") and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            if next_line.startswith("|") and all(c in "|- : " for c in next_line):
                header_line = line
                header_cells = [c.strip() for c in header_line.strip("|").split("|")]
                num_cols = len(header_cells)

                rows = []
                # Header row
                rows.append({
                    "type": "table_row",
                    "table_row": {"cells": [_parse_inline(c) for c in header_cells]},
                })

                i += 2  # skip header and separator

                # Data rows
                while i < len(lines) and lines[i].strip().startswith("|"):
                    data_cells = [c.strip() for c in lines[i].strip("|").split("|")]
                    # Pad or truncate cells to match header column count
                    if len(data_cells) < num_cols:
                        data_cells.extend([""] * (num_cols - len(data_cells)))
                    elif len(data_cells) > num_cols:
                        data_cells = data_cells[:num_cols]

                    rows.append({
                        "type": "table_row",
                        "table_row": {"cells": [_parse_inline(c) for c in data_cells]},
                    })
                    i += 1

                blocks.append({
                    "type": "table",
                    "table": {
                        "table_width": num_cols,
                        "has_column_header": True,
                        "has_row_header": False,
                        "children": rows,
                    },
                })
                continue

        # --- Fenced code block ---
        if stripped.startswith("```"):
            lang = stripped[3:].strip() or "plain text"
            code_lines: List[str] = []
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


def _blocks_to_markdown(blocks: List[dict]) -> str:
    """Convert Notion blocks back to markdown."""
    lines: List[str] = []
    prev_type = ""
    list_index = 0
    table_started = False

    for block in blocks:
        btype = block.get("type", "")
        data = block.get(btype, {})
        rt = data.get("rich_text", [])

        # Track list position for numbered lists
        if btype == "numbered_list_item":
            list_index += 1
        else:
            list_index = 0

        # Add blank line when switching away from list items
        if prev_type in ("bulleted_list_item", "numbered_list_item"):
            if btype not in ("bulleted_list_item", "numbered_list_item"):
                lines.append("")

        # Ensure a blank line after a table ends
        if prev_type == "table_row" and btype != "table_row":
            lines.append("")

        match btype:
            case "table":
                table_started = True
                continue
            case "table_row":
                cells = data.get("cells", [])
                row_str = "| " + " | ".join(_rt_to_md(c) for c in cells) + " |"
                lines.append(row_str)
                if table_started:
                    # Insert separator after header row
                    sep = "| " + " | ".join(["---"] * len(cells)) + " |"
                    lines.append(sep)
                    table_started = False
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
                lines.append(f"{list_index}. {_rt_to_md(rt)}")
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


def _rt_to_md(rich_text: List[dict]) -> str:
    """Convert Notion rich_text array to markdown with formatting."""
    parts: List[str] = []
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


def _rt_to_plain(rich_text: List[dict]) -> str:
    """Extract plain text from rich_text (no formatting)."""
    return "".join(seg.get("text", {}).get("content", "") for seg in rich_text)


def _blocks_to_plain(blocks: List[dict]) -> str:
    """Convert Notion blocks to a single plain text string for searching."""
    text_parts = []
    for block in blocks:
        btype = block.get("type", "")
        data = block.get(btype, {})
        if "rich_text" in data:
            text_parts.append(_rt_to_plain(data["rich_text"]))
    return "\n".join(text_parts)
