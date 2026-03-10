"""Notion backend — cloud-hosted notes via Notion API.

Single-database approach: all notes live in one Notion database.
Folders are a 'folder' rich_text property. Paths are a 'folio_path' text property.
The path→page_id mapping is bulk-loaded on startup (1 API call per 100 notes)
and kept in sync via an in-memory cache.
"""

from __future__ import annotations

import re
import sys
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, List

from notion_client import Client as NotionClient
from notion_client.errors import APIResponseError

from folio.backends import FolioBackend
from folio.config import NotionConfig
from folio.models import Note, NoteSummary, SearchResult, slugify_title as _slugify_title
from folio.sections import extract_section, strip_redundant_heading

logger = logging.getLogger("folio.notion")


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
        """Bulk-load ALL path→page_id mappings on startup."""
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
        """Extract title from a page object."""
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

        if folder:
            props["folder"] = {"rich_text": [{"text": {"content": folder}}]}

        return props

    # ------------------------------------------------------------------
    # Page content I/O (Native Markdown)
    # ------------------------------------------------------------------

    def _read_page_content(self, page_id: str) -> str:
        """Read all content natively as enhanced markdown."""
        try:
            response = self.client.request(
                path=f"pages/{page_id}/markdown",
                method="GET",
            )
            return response.get("page_markdown", {}).get("markdown", "")
        except APIResponseError as e:
            logger.warning(f"Failed to read native markdown for {page_id}: {e}")
            return ""

    def _write_page_content(self, page_id: str, markdown: str, old_content: str | None = None) -> None:
        """Replace all page content surgically by matching the old content string."""
        if not markdown:
            # Full clear requested
            self._clear_all_blocks(page_id)
            return

        # 1. Read current content (1-2 calls) to get exact match string if not provided
        if old_content is None:
            try:
                old_content = self._read_page_content(page_id)
            except Exception:
                old_content = ""

        # 2. If already empty, just insert
        if not old_content.strip():
            self._append_page_content(page_id, markdown)
            return

        # 3. Surgical overwrite in 1 call
        try:
            self.client.request(
                path=f"pages/{page_id}/markdown",
                method="PATCH",
                body={
                    "type": "replace_content_range",
                    "replace_content_range": {
                        "content_range": old_content,
                        "content": markdown.replace("\\n", "\n")
                    }
                }
            )
        except APIResponseError as e:
            logger.warning(f"Surgical overwrite failed, falling back to block-clearing: {e}")
            self._clear_all_blocks(page_id)
            self._append_page_content(page_id, markdown)

    def _prepend_page_content(self, page_id: str, markdown: str) -> None:
        """Prepend markdown to the beginning of the page."""
        if not markdown:
            return

        try:
            old_content = self._read_page_content(page_id)
        except Exception:
            old_content = ""

        content = markdown.replace("\\n", "\n")
        if old_content:
            if not content.endswith("\n\n"):
                content += "\n\n"
            new_content = content + old_content
        else:
            new_content = content

        self._write_page_content(page_id, new_content, old_content)

    def _clear_all_blocks(self, page_id: str) -> None:
        """Delete all blocks on the page using block-by-block deletion."""
        cursor = None
        while True:
            response = self.client.blocks.children.list(
                block_id=page_id,
                start_cursor=cursor,
                page_size=100,
            )
            for b in response["results"]:
                if not b.get("archived") and not b.get("in_trash"):
                    try:
                        self.client.request(path=f"blocks/{b['id']}", method="DELETE")
                    except Exception:
                        continue

            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")

    def _append_page_content(self, page_id: str, markdown: str) -> None:
        """Append markdown to end of page in a single call."""
        if not markdown:
            return
            
        content = markdown.replace("\\n", "\n")
        if not content.startswith("\n"):
            content = "\n" + content

        self.client.request(
            path=f"pages/{page_id}/markdown",
            method="PATCH",
            body={
                "type": "insert_content",
                "insert_content": {
                    "content": content
                }
            }
        )

    def _update_section(self, page_id: str, target: str, content: str) -> None:
        """Surgically update a section using ellipsis-based anchoring."""
        # 1. Fetch current markdown
        full_content = self._read_page_content(page_id)
        
        # 2. Identify the range start and end markers
        lines = full_content.split("\n")
        target_line_idx = -1
        next_heading_idx = -1
        
        target_norm = target.strip().lower()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#"):
                header_text = stripped.lstrip("#").strip().lower()
                if header_text == target_norm:
                    target_line_idx = i
                    for j in range(i + 1, len(lines)):
                        if lines[j].strip().startswith("#"):
                            next_heading_idx = j
                            break
                    break

        if target_line_idx == -1:
            logger.info(f"Section '{target}' not found, appending.")
            self._append_page_content(page_id, f"\n## {target}\n\n{content}")
            return

        content_fixed = content.replace("\\n", "\n")
        clean_body = strip_redundant_heading(content_fixed, target)
        
        actual_heading = lines[target_line_idx]
        new_text = f"{actual_heading}\n\n{clean_body}\n"

        if next_heading_idx != -1:
            end_marker = lines[next_heading_idx]
            content_range = f"{actual_heading}...{end_marker}"
            new_text += f"\n{end_marker}"
        else:
            # Last section: match from heading to the very last line of the page
            non_empty_lines = [l for l in lines if l.strip()]
            if non_empty_lines:
                last_line = non_empty_lines[-1]
                if last_line != actual_heading:
                    content_range = f"{actual_heading}...{last_line}"
                else:
                    content_range = actual_heading
            else:
                content_range = actual_heading

        try:
            self.client.request(
                path=f"pages/{page_id}/markdown",
                method="PATCH",
                body={
                    "type": "replace_content_range",
                    "replace_content_range": {
                        "content_range": content_range,
                        "content": new_text
                    }
                }
            )
        except APIResponseError as e:
            logger.warning(f"Surgical ellipsis update failed: {e}. Falling back to full overwrite.")
            from folio.sections import replace_section
            try:
                new_full_content = replace_section(full_content, target, content)
                self._write_page_content(page_id, new_full_content)
            except ValueError:
                self._append_page_content(page_id, f"\n## {target}\n\n{content}")

    def _page_to_note(self, page: dict, content: str | None = None) -> Note:
        """Convert a Notion page + content into a Note."""
        folio_path = self._get_path(page)
        title = self._get_title_from_page(page)

        # We no longer do the complex auto-syncing of paths here!
        # The SyncEngine is now entirely responsible for evaluating
        # auto-linked vs explicit paths using the Supabase history.

        if not folio_path:
            # The only thing we do here is catch completely brand new
            # pages that have no path at all.
            slug = f"untitled-{page['id'][:8]}.md"
            folder = self._get_folder(page)
            folio_path = f"{folder}/{slug}" if folder else slug

            try:
                self.client.pages.update(
                    page_id=page["id"],
                    properties={
                        "folio_path": {"rich_text": [{"text": {"content": folio_path}}]}
                    }
                )
                self._cache[folio_path] = page["id"]
            except Exception as e:
                logger.error(f"Failed to write initial path to Notion: {e}")

        created, updated = self._get_timestamps(page)
        content = self._read_page_content(page["id"])

        return Note(
            path=folio_path,
            title=title,
            content=content,
            tags=self._get_tags_from_page(page),
            created=created,
            updated=updated,
        )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(self, note: Note) -> Note:
        if note.path in self._cache:
            raise FileExistsError(f"Note already exists: {note.path}")

        properties = self._build_properties(note)
        content_fixed = note.content.replace("\\n", "\n") if note.content else ""
        
        body = {
            "parent": {"database_id": self.database_id},
            "properties": properties,
        }
        if content_fixed:
            body["markdown"] = content_fixed

        page = self.client.request(path="pages", method="POST", body=body)
        page_id = page["id"]
        
        print(f"\n[folio] CREATED PAGE: {page.get('url')}\n", file=sys.stderr)
        self._cache[note.path] = page_id

        created, updated = self._get_timestamps(page)
        return note.model_copy(update={"created": created, "updated": updated})

    def read(self, path: str, section: str | None = None) -> Note:
        page_id = self._resolve_page_id(path)
        try:
            page = self.client.pages.retrieve(page_id)
        except APIResponseError:
            self._cache.pop(path, None)
            raise FileNotFoundError(f"Note not found: {path}")

        if page.get("archived"):
            self._cache.pop(path, None)
            raise FileNotFoundError(f"Note was deleted: {path}")

        note = self._page_to_note(page)
        if section:
            section_content = extract_section(note.content, section)
            if section_content is None:
                raise FileNotFoundError(f"Section '{section}' not found in {path}")
            note = note.model_copy(update={"content": section_content})

        return note

    def update(
        self,
        path: str,
        content: str | None,
        mode: str = "replace",
        target: str | None = None,
        tags: List[str] | None = None,
        title: str | None = None,
    ) -> Note:
        page_id = self._resolve_page_id(path)

        try:
            page = self.client.pages.retrieve(page_id)
        except APIResponseError:
            self._cache.pop(path, None)
            raise FileNotFoundError(f"Note not found: {path}")

        if content is not None:
            match mode:
                case "replace":
                    self._write_page_content(page_id, content)
                case "append":
                    self._append_page_content(page_id, content)
                case "prepend":
                    self._prepend_page_content(page_id, content)
                case "section":
                    if not target:
                        raise ValueError("Section update requires a 'target' heading")
                    self._update_section(page_id, target, content)
                case _:
                    raise ValueError(f"Invalid mode: {mode}")

        updates = {}
        if tags is not None:
            updates["tags"] = {"multi_select": [{"name": t} for t in tags]}
        if title is not None:
            updates["Name"] = {"title": [{"text": {"content": title}}]}

        if updates:
            self.client.pages.update(
                page_id=page_id,
                properties=updates,
            )

        page = self.client.pages.retrieve(page_id)
        return self._page_to_note(page)

    def delete(self, path: str) -> None:
        page_id = self._resolve_page_id(path)
        try:
            self.client.pages.update(page_id=page_id, archived=True)
        except APIResponseError as e:
            raise RuntimeError(f"Failed to delete: {str(e)}")
        self._cache.pop(path, None)

    def move(self, source: str, target: str, title: str | None = None) -> Note:
        from folio.models import evaluate_move_title

        page_id = self._resolve_page_id(source)
        if target in self._cache:
            raise FileExistsError(f"Target already exists: {target}")

        try:
            page = self.client.pages.retrieve(page_id)
        except APIResponseError:
            raise FileNotFoundError(f"Note not found: {source}")

        new_folder = str(Path(target).parent) if "/" in target else ""

        # If title wasn't explicitly provided (e.g. not passed down from Supabase),
        # evaluate it ourselves using the current Notion title.
        if title is None:
            old_title = self._get_title_from_page(page)
            new_title = evaluate_move_title(source, old_title, target)
        else:
            new_title = title

        props: dict[str, Any] = {
            "Name": {"title": [{"text": {"content": new_title}}]},
            "folio_path": {"rich_text": [{"text": {"content": target}}]},
        }
        if new_folder:
            props["folder"] = {"rich_text": [{"text": {"content": new_folder}}]}
        else:
            props["folder"] = {"rich_text": []}

        try:
            self.client.pages.update(page_id=page_id, properties=props)
        except APIResponseError as e:
            raise RuntimeError(f"Failed to move: {str(e)}")

        self._cache.pop(source, None)
        self._cache[target] = page_id
        page = self.client.pages.retrieve(page_id)
        return self._page_to_note(page)

    def list(self, folder: str | None = None) -> List[NoteSummary]:
        body: dict[str, Any] = {
            "page_size": 100,
            "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
        }
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
                summaries.append(NoteSummary(
                    path=path,
                    title=self._get_title_from_page(page),
                    tags=self._get_tags_from_page(page),
                    updated=self._get_timestamps(page)[1],
                    size_tokens=0,
                    has_previous=False,
                ))
                self._cache[path] = page["id"]
            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")
        return summaries

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
        and_filters = []
        if query:
            and_filters.append({"property": "title", "title": {"contains": query}})
        if tags:
            for tag in tags:
                and_filters.append({"property": "tags", "multi_select": {"contains": tag}})
        if folder:
            and_filters.append({"property": "folder", "rich_text": {"equals": folder.rstrip("/")}})

        filter_obj = None
        if and_filters:
            filter_obj = {"and": and_filters} if len(and_filters) > 1 else and_filters[0]

        body: dict[str, Any] = {"page_size": min(limit * 2, 100)}
        if filter_obj:
            body["filter"] = filter_obj
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

        for page in pages:
            if page.get("archived"):
                continue
            path = self._get_path(page)
            if not path:
                continue
            updated = self._get_timestamps(page)[1]
            if cutoff and updated < cutoff:
                continue
            results.append(SearchResult(
                note=NoteSummary(
                    path=path,
                    title=self._get_title_from_page(page),
                    tags=self._get_tags_from_page(page),
                    updated=updated,
                    size_tokens=0,
                ),
                snippet=self._get_title_from_page(page),
                score=1.0,
            ))
            if len(results) >= limit + offset:
                break
        return results[offset:offset+limit]

    def undo(self, path: str) -> Note:
        raise RuntimeError(
            f"Undo is not available with the Notion backend. "
            f"Notion maintains its own page history — use Notion's UI "
            f"to view previous versions of '{path}'."
        )

    def export_all(self) -> List[Note]:
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
                    notes.append(self._page_to_note(page))
                except Exception:
                    continue
            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")
        return notes

    def import_all(self, notes: List[Note]) -> None:
        for note in notes:
            if note.path in self._cache:
                self.update(path=note.path, content=note.content, mode="replace", tags=note.tags)
            else:
                self.create(note)


# =========================================================================
# Module-level helpers
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
        raise ValueError(f"Invalid time filter: '{value}'. Use relative ('7d', '24h', 'today') or ISO date.")


