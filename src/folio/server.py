"""Folio MCP Server. Two tools, one backend, zero opinions about storage."""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, Annotated

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

backend: FolioBackend = get_backend(config)
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
        description="Action: create, read, update, delete, move, list, undo"
    )],
    path: Annotated[Optional[str], Field(
        description="Note path, e.g. 'projects/companion.md'. Required except for list."
    )] = None,
    content: Annotated[Optional[str], Field(
        description="Markdown content. Required for create and update."
    )] = None,
    mode: Annotated[Optional[str], Field(
        description="Update mode: 'replace' (default), 'append', or 'section'."
    )] = None,
    target: Annotated[Optional[str], Field(
        description="Heading name for section mode, or destination path for move."
    )] = None,
    tags: Annotated[Optional[List[str]], Field(
        description="Tags for organization. Used with create and update."
    )] = None,
    folder: Annotated[Optional[str], Field(
        description="Folder to list. Only used with action='list'."
    )] = None,
    section: Annotated[Optional[str], Field(
        description="Heading to read. Only used with action='read'."
    )] = None,
) -> Dict[str, Any]:
    """Create, read, update, delete, move, list, or undo notes.
    Markdown files organized in folders with automatic versioning.

    Examples:
        Create:  action='create', path='journal/today.md', content='# Feb 23\\nGood day.'
        Read:    action='read', path='journal/today.md'
        Read §:  action='read', path='journal/today.md', section='Afternoon'
        Append:  action='update', path='journal/today.md', content='\\n- new item', mode='append'
        Section: action='update', path='notes/project.md', content='new text', mode='section', target='Status'
        Delete:  action='delete', path='journal/today.md'
        Move:    action='move', path='journal/today.md', target='archive/today.md'
        List:    action='list', folder='journal'
        Undo:    action='undo', path='journal/today.md'
    """
    try:
        match action:
            # ------ CREATE ------
            case "create":
                if not path:
                    return {"error": "path is required for create"}
                if content is None:
                    return {"error": "content is required for create"}
                note = Note(path=path, content=content, tags=tags or [])
                result = backend.create(note)
                return _note_response(result, status="created")

            # ------ READ ------
            case "read":
                if not path:
                    return {"error": "path is required for read"}
                result = backend.read(path, section=section)
                return _note_response(result, status="read")

            # ------ UPDATE ------
            case "update":
                if not path:
                    return {"error": "path is required for update"}
                update_mode = mode or "replace"
                if update_mode not in ("replace", "append", "section"):
                    return {"error": f"Invalid mode: {update_mode}. Use replace, append, or section."}
                if update_mode == "section" and not target:
                    return {"error": "target (heading name) is required for section mode"}
                if content is None:
                    return {"error": "content is required for update"}
                result = backend.update(
                    path=path,
                    content=content,
                    mode=update_mode,
                    target=target,
                    tags=tags,
                )
                return _note_response(result, status="updated")

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
                if not target:
                    return {"error": "target path is required for move"}
                result = backend.move(source=path, target=target)
                return _note_response(result, status="moved")

            # ------ LIST ------
            case "list":
                notes = backend.list(folder=folder)
                return {
                    "status": "ok",
                    "folder": folder or "/",
                    "notes": [_summary_dict(n) for n in notes],
                    "count": len(notes),
                }

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
    limit: Annotated[Optional[int], Field(
        description="Max results to return (default: 10)"
    )] = None,
) -> Dict[str, Any]:
    """Search across all notes by content, filename, tags, and time.

    Examples:
        Basic:     query='architecture decisions'
        Tagged:    query='todo', tags=['urgent']
        Scoped:    query='deployment', folder='projects'
        Recent:    query='meeting', sort='recent', updated_since='7d'
        Limited:   query='ideas', limit=5
    """
    try:
        results = backend.search(
            query=query,
            tags=tags,
            folder=folder,
            sort=sort or "relevance",
            updated_since=updated_since,
            limit=limit or 10,
        )
        return {
            "status": "ok",
            "query": query,
            "results": [
                {
                    **_summary_dict(r.note),
                    "snippet": r.snippet,
                    "score": round(r.score, 3),
                }
                for r in results
            ],
            "count": len(results),
        }

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
