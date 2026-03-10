import re
import threading
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, List

from supabase import create_client, Client
import postgrest.exceptions

from folio.backends import FolioBackend
from folio.config import SupabaseConfig
from folio.models import Note, NoteSummary, SearchResult
from folio.sections import extract_section, replace_section

class SupabaseBackend(FolioBackend):
    def __init__(self, config: SupabaseConfig, notion_backend=None, sync_engine=None):
        self.client: Client = create_client(config.url, config.key)
        self.notion = notion_backend
        self.sync_engine = sync_engine
        self.logger = logging.getLogger("folio.supabase")

    def _push_to_notion(self, operation: str, **kwargs):
        """Fire-and-forget push to Notion in a background thread."""
        if not self.notion:
            return
        
        def _do_push():
            try:
                if operation == "create":
                    note = kwargs["note"]
                    self.notion.create(note)
                    # Resolve the Notion page_id from the backend's internal cache
                    ext_id = self.notion._cache.get(note.path)
                    if ext_id:
                        self.client.table("notes").update({
                            "external_id": ext_id,
                            "sync_status": "synced",
                            "last_synced_at": datetime.now(timezone.utc).isoformat()
                        }).eq("path", note.path).execute()
                        return
                elif operation == "update":
                    self.notion.update(
                        path=kwargs["path"],
                        content=kwargs.get("content"),
                        mode=kwargs.get("mode", "replace"),
                        target=kwargs.get("target"),
                        tags=kwargs.get("tags"),
                        title=kwargs.get("title"),
                    )
                elif operation == "delete":
                    self.notion.delete(kwargs["path"])
                elif operation == "move":
                    self.notion.move(source=kwargs["source"], target=kwargs["target_path"])
                
                # Mark as synced in Supabase (unless it's a delete waiting for the reconciler)
                if operation != "delete":
                    path = kwargs.get("path") or kwargs.get("note", {}).path
                    if operation == "move":
                        path = kwargs["target_path"]
                    self.client.table("notes").update({
                        "sync_status": "synced",
                        "last_synced_at": datetime.now(timezone.utc).isoformat()
                    }).eq("path", path).execute()
                
            except Exception as e:
                self.logger.error(f"Background Notion push failed ({operation} {kwargs.get('path', '')}): {e}")
                # Note stays as 'pending_push' in Supabase — sync engine can retry later
        
        threading.Thread(target=_do_push, daemon=True).start()

    def _row_to_note(self, row: dict) -> Note:
        return Note(
            path=row["path"],
            title=row.get("title", ""),
            content=row.get("content", ""),
            tags=row.get("tags", []),
            created=datetime.fromisoformat(row["created_at"]),
            updated=datetime.fromisoformat(row["updated_at"]),
            metadata=row.get("metadata", {})
        )

    def create(self, note: Note) -> Note:
        try:
            # Force pre-computation of dynamic properties
            title = note.title
            folder = note.folder
            
            data = {
                "path": note.path,
                "title": title,
                "content": note.content,
                "tags": note.tags,
                "folder": folder,
                "size_tokens": note.size_tokens,
                "created_at": note.created.isoformat(),
                "updated_at": note.updated.isoformat(),
                "sync_status": "pending_push" if self.sync_engine else "synced",
                "metadata": note.metadata
            }
            res = self.client.table("notes").insert(data).execute()
            result = self._row_to_note(res.data[0])
            self._push_to_notion("create", note=note, path=note.path)
            return result
        except postgrest.exceptions.APIError as e:
            if "duplicate key value" in str(e):
                raise FileExistsError(f"Note already exists: {note.path}")
            raise RuntimeError(f"Database error: {e}")

    def read(self, path: str, section: str | None = None) -> Note:
        res = self.client.table("notes").select("*").eq("path", path).neq("sync_status", "pending_delete").execute()
        if not res.data:
            raise FileNotFoundError(f"Note not found: {path}")
            
        note = self._row_to_note(res.data[0])
        
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
        note = self.read(path)
        
        match mode:
            case "replace":
                new_content = content if content is not None else note.content
            case "append":
                new_content = note.content + "\n" + content if content is not None else note.content
            case "prepend":
                new_content = content + "\n\n" + note.content if content is not None else note.content
            case "section":
                new_content = replace_section(note.content, target, content)
            case _:
                raise ValueError(f"Invalid mode: {mode}")

        updated_note = note.model_copy(update={
            "content": new_content,
            "tags": tags if tags is not None else note.tags,
            "updated": datetime.now(timezone.utc)
        })

        data = {
            "content": updated_note.content,
            "tags": updated_note.tags,
            "title": updated_note.title,
            "size_tokens": updated_note.size_tokens,
            "sync_status": "pending_push" if self.sync_engine else "synced",
            "updated_at": updated_note.updated.isoformat()
        }
        
        res = self.client.table("notes").update(data).eq("path", path).execute()
        result = self._row_to_note(res.data[0])
        self._push_to_notion("update", path=path, content=content, mode=mode, target=target, tags=tags, title=title)
        return result

    def delete(self, path: str) -> None:
        # Verify exists
        self.read(path)
        
        if self.sync_engine:
            self.client.table("notes").update({"sync_status": "pending_delete"}).eq("path", path).execute()
        else:
            self.client.table("notes").delete().eq("path", path).execute()
        
        self._push_to_notion("delete", path=path)

    def move(self, source: str, target: str) -> Note:
        from folio.models import evaluate_move_title

        note = self.read(source)
        new_folder = "/".join(target.split("/")[:-1]) if "/" in target else ""
        new_title = evaluate_move_title(source, note.title, target)
        
        data = {
            "path": target,
            "title": new_title,
            "folder": new_folder,
            "sync_status": "pending_push" if self.sync_engine else "synced",
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        try:
            res = self.client.table("notes").update(data).eq("path", source).execute()
            result = self._row_to_note(res.data[0])
            self._push_to_notion("move", source=source, target_path=target, path=target, title=new_title)
            return result
        except postgrest.exceptions.APIError as e:
            if "duplicate key value" in str(e):
                raise FileExistsError(f"Target already exists: {target}")
            raise RuntimeError(f"Database error: {e}")

    def list(self, folder: str | None = None) -> List[NoteSummary]:
        query = self.client.table("notes").select("path, title, tags, updated_at, size_tokens").neq("sync_status", "pending_delete")
        if folder:
            query = query.eq("folder", folder.rstrip("/"))
            
        res = query.order("updated_at", desc=True).limit(10000).execute()
        
        return [
            NoteSummary(
                path=row["path"],
                title=row["title"],
                tags=row["tags"],
                updated=datetime.fromisoformat(row["updated_at"]),
                size_tokens=row["size_tokens"],
                has_previous=False
            ) for row in res.data
        ]

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
        
        cutoff = _parse_since(updated_since).isoformat() if updated_since else None
        
        res = self.client.rpc('search_notes', {
            'search_term': query,
            'filter_folder': folder.rstrip("/") if folder else None,
            'filter_tags': tags,
            'filter_since': cutoff,
            'sort_by': sort,
            'max_results': limit,
            'page_offset': offset
        }).execute()

        results = []
        for row in res.data:
            summary = NoteSummary(
                path=row["path"],
                title=row["title"],
                tags=row["tags"],
                updated=datetime.fromisoformat(row["updated_at"]),
                size_tokens=row["size_tokens"],
                has_previous=False
            )
            results.append(SearchResult(
                note=summary,
                snippet=row["snippet"],
                score=row["score"]
            ))
            
        return results

    def undo(self, path: str) -> Note:
        raise RuntimeError("Undo not yet supported for Supabase backend. Row-level version history coming soon.")

    def export_all(self) -> List[Note]:
        all_notes = []
        offset = 0
        batch_size = 1000
        while True:
            res = (
                self.client.table("notes")
                .select("*")
                .neq("sync_status", "pending_delete")
                .range(offset, offset + batch_size - 1)
                .execute()
            )
            all_notes.extend(self._row_to_note(row) for row in res.data)
            if len(res.data) < batch_size:
                break
            offset += batch_size
        return all_notes

    def import_all(self, notes: List[Note]) -> None:
        for note in notes:
            data = {
                "path": note.path,
                "title": note.title,
                "content": note.content,
                "tags": note.tags,
                "folder": note.folder,
                "size_tokens": note.size_tokens,
                "created_at": note.created.isoformat(),
                "updated_at": note.updated.isoformat(),
                "sync_status": "pending_push" if self.sync_engine else "synced",
                "metadata": note.metadata
            }
            self.client.table("notes").upsert(data, on_conflict="path").execute()

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
