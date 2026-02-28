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
from folio.models import Note, NoteSummary, SearchResult
from folio.sections import extract_section, strip_redundant_heading

logger = logging.getLogger("folio.notion")


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
        """Replace all page content using native Markdown API."""
        if not markdown:
            # If empty, we still have to use block API to clear
            blocks = self._fetch_all_blocks(page_id)
            for b in blocks:
                self.client.request(path=f"blocks/{b['id']}", method="DELETE")
            return

        content = markdown.replace("\\n", "\n")
        self.client.request(
            path=f"pages/{page_id}/markdown",
            method="PATCH",
            body={
                "type": "replace_content_range",
                "replace_content_range": {
                    "content_range": "...", # Entire page
                    "content": content
                }
            }
        )

    def _append_page_content(self, page_id: str, markdown: str) -> None:
        """Append markdown to end of page using native Markdown API."""
        if not markdown:
            return
            
        content = markdown.replace("\\n", "\n")
        # Ensure we start on a new line
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
        """Update a section using native Markdown ellipsis matching."""
        content_fixed = content.replace("\\n", "\n")
        # Strip redundant heading if it matches the target
        clean_body = strip_redundant_heading(content_fixed, target)
        
        # We assume the heading exists. We replace from heading start to end of section.
        # This is a bit tricky with native matching if we don't know the exact heading text.
        # But we can try to match the heading text itself.
        
        # Try matching any heading level with the target text
        self.client.request(
            path=f"pages/{page_id}/markdown",
            method="PATCH",
            body={
                "type": "replace_content_range",
                "replace_content_range": {
                    # This is the "be liberal" native version: 
                    # matches the heading text and everything until the next heading or EOF
                    "content_range": f"{target}...#", 
                    "content": f"{target}\n\n{clean_body}\n\n#" 
                }
            }
        )
        # Note: The above is a heuristic. If it fails, we fall back to full page write.
        # Since we can't read markdown natively to find exact offsets, 
        # the most reliable way for internal integrations is often full replace.
        
    def _page_to_note(self, page: dict, content: str | None = None) -> Note:
        """Convert a Notion page + content into a Note."""
        folio_path = self._get_path(page)
        title = self._get_title_from_page(page)
        folder = self._get_folder(page)

        # Path derivation for notes created directly in Notion UI
        if not folio_path:
            slug = _slugify_title(title) if title else f"untitled-{page['id'][:8]}.md"
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
                logger.error(f"Failed to write derived path back to Notion: {e}")

        created, updated = self._get_timestamps(page)
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
                case "section":
                    # For now, full replace is safer than heuristic matching 
                    # since we can't read markdown natively to verify the match
                    note = self._page_to_note(page)
                    from folio.sections import replace_section
                    new_full_content = replace_section(note.content, target, content)
                    self._write_page_content(page_id, new_full_content)
                case _:
                    raise ValueError(f"Invalid mode: {mode}")

        if tags is not None:
            self.client.pages.update(
                page_id=page_id,
                properties={"tags": {"multi_select": [{"name": t} for t in tags]}},
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

    def move(self, source: str, target: str) -> Note:
        page_id = self._resolve_page_id(source)
        if target in self._cache:
            raise FileExistsError(f"Target already exists: {target}")

        new_folder = str(Path(target).parent) if "/" in target else ""
        new_stem = Path(target).stem
        new_title = new_stem.replace("-", " ").replace("_", " ").title()

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
            if len(results) >= limit:
                break
        return results

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

        if btype == "numbered_list_item":
            list_index += 1
        else:
            list_index = 0

        if prev_type in ("bulleted_list_item", "numbered_list_item"):
            if btype not in ("bulleted_list_item", "numbered_list_item"):
                lines.append("")

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
