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
        description="Markdown content for create/update. Omit on update to keep existing."
    )] = None,
    mode: Annotated[Optional[str], Field(
        description="Update mode: 'replace' (default), 'append', or 'section'"
    )] = None,
    target: Annotated[Optional[str], Field(
        description="Heading name for mode='section' (no # prefix)"
    )] = None,
    destination: Annotated[Optional[str], Field(
        description="New path for action='move'"
    )] = None,
    destination: Annotated[Optional[str], Field(
        description="New path for action='move'"
    )] = None,
    tags: Annotated[Optional[List[str]], Field(
        description="Tags for organization. Used with create and update."
    )] = None,
    folder: Annotated[Optional[str], Field(
        description="Folder to list. Only used with action='list'."
    )] = None,
    section: Annotated[Optional[str], Field(
        description="Read only this section (heading text, no # prefix)"
    )] = None,
) -> Dict[str, Any]:
    """Markdown notes in folders with tags and versioning.
    Use append for running logs, section to refresh one heading, replace to rewrite.

    Examples:
        Create:  action='create', path='journal/2026-02-23.md', tags=['journal'],
                 content='# Sunday\\n## Morning\\nCycled in -6°C...\\n## Evening\\n...'
        Read section:  action='read', path='people/him❤️/plans.md', section='Weekend Cabin Trip'
        Append:  action='update', path='watching/watchlist.md', mode='append',
                 content='\\n- The Terror S1 — slow-burn arctic horror'
        Section: action='update', path='projects/companion.md', mode='section',
                 target='Status', content='Folio MCP complete. Testing phase.'
        Retag:   action='update', path='shows/dark.md', tags=['favorite', 'pinned']
        Move:    action='move', path='notes/pizza.md', destination='food/pizza.md'
        List:    action='list', folder='journal'
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
                if content is None and tags is None:
                    return {"error": "content or tags required for update"}
                if update_mode == "section" and content is None:
                    return {"error": "content is required for section mode"}
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
                if not destination:
                    return {"error": "destination is required for move"}
                result = backend.move(source=path, target=destination)
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
        Content:   query='cycling routes near Malmö'
        Tagged:    query='birthday', tags=['person']
        Scoped:    query='memory architecture', folder='projects'
        Pinned:    query='', tags=['pinned']
        Recent:    query='illustration', sort='recent', updated_since='7d'
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
