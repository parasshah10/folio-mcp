from datetime import datetime
from folio.sync import SyncAdapter
from folio.backends.notion import NotionBackend
from folio.config import NotionConfig
from folio.models import Note

class NotionSyncAdapter(SyncAdapter):
    def __init__(self, config: NotionConfig):
        self._backend = NotionBackend(config)
        
    def push_create(self, note: Note, metadata: dict) -> str:
        # Re-use the backend's create logic, then pluck the ID from cache
        self._backend.create(note)
        return self._backend._cache[note.path]
        
    def push_update(self, external_id: str, note: Note, metadata: dict) -> None:
        self._backend.update(
            path=note.path,
            content=note.content,
            mode="replace",
            tags=note.tags
        )
        
    def push_delete(self, external_id: str) -> None:
        self._backend.client.pages.update(
            page_id=external_id,
            archived=True
        )
        
    def pull_changes(self, since: datetime) -> list[tuple[str, Note, datetime, bool]]:
        changes = []
        cursor = None
        
        while True:
            body = {
                "page_size": 100,
                "filter": {
                    "timestamp": "last_edited_time",
                    "last_edited_time": {
                        "on_or_after": since.isoformat()
                    }
                }
            }
            if cursor:
                body["start_cursor"] = cursor
                
            response = self._backend.client.request(
                path=f"data_sources/{self._backend.data_source_id}/query",
                method="POST",
                body=body,
            )
            
            for page in response.get("results", []):
                in_trash = page.get("archived", False) or page.get("in_trash", False)
                _, updated = self._backend._get_timestamps(page)
                
                if in_trash:
                    changes.append((page["id"], None, updated, True))
                else:
                    try:
                        note = self._backend._page_to_note(page)
                        changes.append((page["id"], note, updated, False))
                    except Exception:
                        continue # Skip malformed pages
                        
            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")
            
        return changes
        
    def pull_all_ids(self) -> list[tuple[str, datetime]]:
        ids = []
        cursor = None
        
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
                
            response = self._backend.client.request(
                path=f"data_sources/{self._backend.data_source_id}/query",
                method="POST",
                body=body,
            )
            
            for page in response.get("results", []):
                if not page.get("archived", False) and not page.get("in_trash", False):
                    _, updated = self._backend._get_timestamps(page)
                    ids.append((page["id"], updated))
                    
            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")
            
        return ids
