"""Folio MCP Server. Two tools, one backend, zero opinions about storage."""

from __future__ import annotations

import sys
import logging
from typing import Any, Dict, List, Optional, Annotated

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger("folio.server")

from pathlib import Path

from fastmcp import FastMCP
from pydantic import Field

from folio.config import FolioConfig
from folio.models import Note, NoteSummary
from folio.backends import get_backend, FolioBackend

# ---------------------------------------------------------------------------
# Server + Config
# ---------------------------------------------------------------------------
config = FolioConfig.from_env()
errors = config.validate_backend()
if errors:
    for e in errors:
        print(f"[folio] Config error: {e}", file=sys.stderr)
    sys.exit(1)

import threading

backend: FolioBackend = get_backend(config)

# Start background sync engine if configured
if hasattr(backend, "sync_engine") and backend.sync_engine:
    def _run_sync():
        backend.sync_engine.run_loop(config.sync.interval_seconds)
    threading.Thread(target=_run_sync, daemon=True).start()

mcp = FastMCP("folio")


# ---------------------------------------------------------------------------
# Response Helpers
# ---------------------------------------------------------------------------

def _note_response(note: Note, status: str = "ok") -> Dict[str, Any]:
    """Format a Note into a tool response dict."""
    resp: Dict[str, Any] = {
        "status": status,
        "path": note.path,
        "title": note.title,
        "tags": note.tags,
        "created": note.created.isoformat(),
        "updated": note.updated.isoformat(),
        "size_tokens": note.size_tokens,
    }
    if status in ("ok", "read", "undone"):
        resp["content"] = note.content
        if note.size_tokens > config.warn_tokens:
            resp["size_warning"] = (
                f"Note is {note.size_tokens} tokens (threshold: {config.warn_tokens}). "
                "Consider splitting into smaller notes or reading by section."
            )
    return resp


def _summary_dict(s: NoteSummary) -> Dict[str, Any]:
    return {
        "path": s.path,
        "title": s.title,
        "tags": s.tags,
        "updated": s.updated.isoformat(),
        "size_tokens": s.size_tokens,
        "has_previous": s.has_previous,
    }


# ---------------------------------------------------------------------------
# Tool 1: folio — CRUD + list + undo
# ---------------------------------------------------------------------------

@mcp.tool
def folio(
    action: Annotated[str, Field(
        description="Action: create, read, update, delete, move, list, toc, undo"
    )],
    path: Annotated[Optional[str], Field(
        description="Note path, e.g. 'projects/companion.md'. Required except for list."
    )] = None,
    content: Annotated[Optional[str], Field(
        description="Markdown content. For section mode, provide only the section body — the heading is preserved automatically."
    )] = None,
    mode: Annotated[Optional[str], Field(
        description="Update mode: 'replace' (default), 'append', 'prepend', or 'section'"
    )] = None,
    target: Annotated[Optional[str], Field(
        description="Heading name for mode='section' (no # prefix)"
    )] = None,
    destination: Annotated[Optional[str], Field(
        description="New path for action='move'"
    )] = None,
    tags: Annotated[Optional[Any], Field(
        description="Tags for organization. Used with create and update. Can be a list or comma-separated string."
    )] = None,
    folder: Annotated[Optional[str], Field(
        description="Folder to list. Only used with action='list'."
    )] = None,
    section: Annotated[Optional[str], Field(
        description="Read only this section (heading text, no # prefix)"
    )] = None,
    limit: Annotated[int, Field(
        description="Max items to list (default 100). Only used with action='list'."
    )] = 100,
    page: Annotated[int, Field(
        description="Page number for pagination. Default is 1. Only used with action='list'."
    )] = 1,
    sort: Annotated[Optional[str], Field(
        description="Sort order: 'updated' (default), 'name', or 'size'. Only used with action='list'."
    )] = None,
) -> Dict[str, Any]:
    """Markdown notes in folders with tags and versioning.
    Use prepend for running logs, section to refresh one heading body, replace to rewrite.

    Examples:
        Create:  action='create', path='journal/2026-02-23.md', tags=['journal'],
                 content='# Sunday\\n## Morning\\nCycled in -6°C...\\n## Evening\\n...'
        Read section:  action='read', path='plans/cabin-weekend.md', section='Weekend Cabin Trip'
        Table of Contents: action='toc', path='huge-note.md' (Returns headings and their sizes)
        Append:  action='update', path='watching/watchlist.md', mode='append',
                 content='\\n- The Terror S1 — slow-burn arctic horror'
        Section: action='update', path='projects/companion.md', mode='section',
                 target='Status', content='Folio MCP complete. Testing phase.'
        Retag:   action='update', path='shows/dark.md', tags=['favorite', 'pinned']
        Move:    action='move', path='notes/pizza.md', destination='food/pizza.md'
        List:    action='list', folder='journal', page=2
    """
    try:
        # Normalize tags
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        
        match action:
            # ------ CREATE ------
            case "create":
                if not path:
                    return {"error": "path is required for create"}
                if content is None:
                    return {"error": "content is required for create"}
                try:
                    note = Note(path=path, content=content, tags=tags or [])
                    result = backend.create(note)
                    return _note_response(result, status="created")
                except FileExistsError:
                    existing = backend.read(path)
                    return {
                        "error": "Note already exists. Use update to modify.",
                        "existing": _note_response(existing, status="read")
                    }

            # ------ READ ------
            case "read":
                if not path:
                    return {"error": "path is required for read"}
                try:
                    result = backend.read(path, section=section)
                    return _note_response(result, status="read")
                except FileNotFoundError as e:
                    # Check if it was the file or the section that was missing
                    if section:
                        try:
                            # Try reading full note to see if file exists
                            full_note = backend.read(path)
                            from folio.sections import list_headings
                            headings = list_headings(full_note.content)
                            return {
                                "error": f"Section '{section}' not found in {path}",
                                "available_sections": [h["text"] for h in headings]
                            }
                        except FileNotFoundError:
                            pass # Fall through to generic file not found
                    raise e

            # ------ UPDATE ------
            case "update":
                if not path:
                    return {"error": "path is required for update"}
                update_mode = mode or "replace"
                if update_mode not in ("replace", "append", "prepend", "section"):
                    return {"error": f"Invalid mode: {update_mode}. Use replace, append, prepend, or section."}
                if update_mode == "section" and not target:
                    return {"error": "target (heading name) is required for section mode"}
                if content is None and tags is None:
                    return {"error": "content or tags required for update"}
                if update_mode == "section" and content is None:
                    return {"error": "content is required for section mode"}
                try:
                    result = backend.update(
                        path=path,
                        content=content,
                        mode=update_mode,
                        target=target,
                        tags=tags,
                    )
                    return _note_response(result, status="updated")
                except (FileNotFoundError, ValueError) as e:
                    if update_mode == "section":
                        try:
                            # If file exists, list headings to help caller
                            full_note = backend.read(path)
                            from folio.sections import list_headings
                            headings = list_headings(full_note.content)
                            return {
                                "error": f"Section '{target}' not found in {path}",
                                "available_sections": [h["text"] for h in headings]
                            }
                        except FileNotFoundError:
                            pass
                    raise e
                
            # ------ DELETE ------
            case "delete":
                if not path:
                    return {"error": "path is required for delete"}
                backend.delete(path)
                return {"status": "deleted", "path": path}

            # ------ MOVE ------
            case "move":
                if not path:
                    return {"error": "path is required for move"}
                if not destination:
                    return {"error": "destination is required for move"}
                try:
                    result = backend.move(source=path, target=destination)
                    return _note_response(result, status="moved")
                except FileExistsError:
                    existing = backend.read(destination)
                    return {
                        "error": "Target already exists. Delete or rename it first.",
                        "existing": _summary_dict(NoteSummary.from_note(existing))
                    }

            # ------ LIST ------
            case "list":
                offset = (max(1, page) - 1) * limit
                all_notes = backend.list()

                # Check if the workspace uses folders at all
                has_folders = any(n.path.count("/") > 0 for n in all_notes)

                # Filter notes by folder if requested
                if folder:
                    folder_path = folder.strip("/")
                    notes = [n for n in all_notes if str(Path(n.path).parent) == folder_path or (not folder_path and "/" not in n.path)]
                else:
                    notes = all_notes

                # If no folder requested, and there are folders in the workspace, show the Root Map
                if not folder and has_folders:
                    from collections import defaultdict
                    grouped = defaultdict(list)
                    for n in notes:
                        p = str(Path(n.path).parent) if "/" in n.path else ""
                        grouped[p].append(n)

                    # Sort folders by their most recently updated note, limit to top 100
                    sorted_folders = sorted(
                        grouped.items(),
                        key=lambda x: max((n.updated for n in x[1])),
                        reverse=True
                    )

                    total_folders = len(sorted_folders)
                    folders_page = sorted_folders[offset:offset+limit]

                    lines = [f"Workspace Map (Showing folders {offset + 1}-{offset + len(folders_page)} of {total_folders}):", ""]
                    for f_name, f_notes in folders_page:
                        # Sort by updated desc
                        f_notes.sort(key=lambda x: x.updated, reverse=True)
                        f_disp = f"{f_name}/" if f_name else "/"
                        latest = [Path(n.path).name for n in f_notes[:3]]
                        latest_str = ", ".join(latest)
                        if len(f_notes) > 3:
                            latest_str += "..."
                        lines.append(f"{f_disp} ({len(f_notes)} notes) - Latest: {latest_str}")

                    if total_folders > offset + limit:
                        lines.append("")
                        lines.append(f"... {total_folders - (offset + limit)} more folders. Use `page={page + 1}` to view more folders.")

                    lines.append("")
                    lines.append("Tip: Use `action='list', folder='<name>'` to zoom into a folder.")
                    return {"status": "ok", "format": "text", "text": "\n".join(lines)}

                # Otherwise, show Zoomed View for the folder (or flat workspace)
                sort_mode = (sort or "updated").lower()
                if sort_mode == "name":
                    notes.sort(key=lambda x: Path(x.path).name.lower())
                elif sort_mode == "size":
                    notes.sort(key=lambda x: x.size_tokens, reverse=True)
                else: # "updated" default
                    notes.sort(key=lambda x: x.updated, reverse=True)

                total_notes = len(notes)
                notes_page = notes[offset:offset+limit]

                f_disp = f"{folder.strip('/')}/" if folder else "/"
                lines = [f"Folder: {f_disp} (Sorted by {sort_mode})"]
                lines.append(f"Showing {len(notes_page)} notes (Page {page}) of {total_notes} total.")
                lines.append("")

                for n in notes_page:
                    filename = Path(n.path).name
                    tag_str = f" [tags: {', '.join(n.tags)}]" if n.tags else ""
                    # Convert to K if > 1000
                    toks = f"{n.size_tokens/1000:.1f}k" if n.size_tokens > 1000 else str(n.size_tokens)
                    updated = n.updated.strftime("%Y-%m-%d")
                    lines.append(f"{filename} ({toks} tokens){tag_str} - Updated: {updated}")

                if total_notes > offset + limit:
                    lines.append("")
                    lines.append(f"... {total_notes - (offset + limit)} more notes. Use `page={page + 1}` to view older notes.")

                return {"status": "ok", "format": "text", "text": "\n".join(lines)}

            # ------ TOC ------
            case "toc":
                if not path:
                    return {"error": "path is required for toc"}
                try:
                    note = backend.read(path)
                except FileNotFoundError as e:
                    return {"error": str(e)}

                from folio.sections import list_headings
                headings = list_headings(note.content)
                if not headings:
                    return {"status": "ok", "format": "text", "text": f"No headings found in {path} ({note.size_tokens} tokens total)."}

                lines = [f"Table of Contents for {path} ({note.size_tokens} tokens total):", ""]
                for h in headings:
                    indent = "  " * (h["level"] - 1)
                    # Approximate token size of the section
                    size = h.get("length", 0) // 4
                    toks = f"{size/1000:.1f}k" if size > 1000 else str(size)
                    lines.append(f"{indent}{'#' * h['level']} {h['text']} ({toks} tokens)")

                return {"status": "ok", "format": "text", "text": "\n".join(lines)}

            # ------ UNDO ------
            case "undo":
                if not path:
                    return {"error": "path is required for undo"}
                result = backend.undo(path)
                return _note_response(result, status="undone")

            # ------ UNKNOWN ------
            case _:
                return {
                    "error": f"Unknown action: {action}. "
                    "Use: create, read, update, delete, move, list, undo"
                }

    except FileNotFoundError:
        return {"error": f"Note not found: {path}"}
    except FileExistsError:
        return {"error": f"Note already exists: {path}. Use update to modify."}
    except PermissionError as e:
        return {"error": f"Permission denied: {e}"}
    except Exception as e:
        return {"error": f"Unexpected error: {type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Tool 2: folio_search — Find notes by content, tags, time
# ---------------------------------------------------------------------------

@mcp.tool
def folio_search(
    query: Annotated[str, Field(
        description="Search text, matched against content and filenames"
    )],
    tags: Annotated[Optional[List[str]], Field(
        description="Filter to notes with ALL of these tags"
    )] = None,
    folder: Annotated[Optional[str], Field(
        description="Limit search to this folder"
    )] = None,
    sort: Annotated[Optional[str], Field(
        description="Sort by 'relevance' (default) or 'recent'"
    )] = None,
    updated_since: Annotated[Optional[str], Field(
        description="Time filter: ISO date or relative like '7d', '24h', 'today'"
    )] = None,
    limit: Annotated[int, Field(
        description="Max results to return per page (default: 10)"
    )] = 10,
    page: Annotated[int, Field(
        description="Page number for pagination. Default is 1."
    )] = 1,
) -> Dict[str, Any]:
    """Search across all notes by content, filename, tags, and time.

    Examples:
        Content:   query='cycling routes near Malmö'
        Tagged:    query='birthday', tags=['person']
        Scoped:    query='memory architecture', folder='projects'
        Pinned:    query='', tags=['pinned']
        Recent:    query='illustration', sort='recent', updated_since='7d'
        Paginate:  query='react', page=2
    """
    try:
        offset = (max(1, page) - 1) * limit
        results = backend.search(
            query=query,
            tags=tags,
            folder=folder,
            sort=sort or "relevance",
            updated_since=updated_since,
            limit=limit,
            offset=offset,
        )

        if not results:
            return {"status": "ok", "format": "text", "text": f"No results found for '{query}'"}

        lines = [f"Search Results for \"{query}\" (Showing page {page}):", ""]

        for i, r in enumerate(results):
            filename = Path(r.note.path).name
            tag_str = f" [tags: {', '.join(r.note.tags)}]" if r.note.tags else ""
            toks = f"{r.note.size_tokens/1000:.1f}k" if r.note.size_tokens > 1000 else str(r.note.size_tokens)
            updated = r.note.updated.strftime("%Y-%m-%d")
            score = round(r.score, 2)

            lines.append(f"{offset + i + 1}. {filename} ({r.note.path})")
            lines.append(f"   [{toks} tokens]{tag_str} | Updated: {updated} | Score: {score}")

            snippet = r.snippet.replace("\n", " ").strip()
            if snippet:
                lines.append(f"   \"{snippet}\"")
            lines.append("")

        if len(results) == limit:
            lines.append(f"Use `page={page + 1}` to view more results.")

        return {"status": "ok", "format": "text", "text": "\n".join(lines).strip()}

    except Exception as e:
        return {"error": f"Search failed: {type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Main entry point for the Folio MCP server."""
    print(f"[folio] Starting with backend: {config.backend}", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
